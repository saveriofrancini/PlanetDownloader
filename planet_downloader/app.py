"""Loopback-only GUI server. Search and billable orders are separate actions."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile

from flask import Flask, jsonify, render_template, request, Response

from .config import DEFAULTS, validate_config

MAX_UPLOAD = 256 * 1024 * 1024
VECTOR_EXTS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".geojson", ".json", ".gpkg"}


def atomic_json(path: Path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def safe_error(exc, key=""):
    text = str(exc)
    if key:
        text = text.replace(key, "[chiave omessa]")
    # Never display signed URLs accidentally included by a transport error.
    import re
    text = re.sub(r"https?://[^\s\"'<>]+", "[indirizzo remoto omesso]", text)
    return text[:1800] or type(exc).__name__


def compact_plan(plan):
    if not plan:
        return None
    result = {k: plan.get(k) for k in ("version", "created_at", "config", "totals", "warnings")}
    result["areas"] = []
    for area in plan.get("areas", []):
        item = {k: area.get(k) for k in ("id", "name", "area_km2", "warnings")}
        item["scenes"] = [{k: s.get(k) for k in ("id", "acquired", "date", "cloud_percent", "clear_percent", "coverage_percent", "aoi_cloud_percent", "aoi_clear_percent")} for s in area.get("scenes", [])]
        item["zones"] = [{k: z.get(k) for k in ("id", "anchor_id")} for z in area.get("zones", [])]
        item["orders"] = [{k: o.get(k) for k in ("key", "zone_id", "anchor_id", "estimated_km2")} for o in area.get("orders", [])]
        result["areas"].append(item)
    return result


def create_app(work_dir=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
    token = secrets.token_urlsafe(32)
    upload_dir = Path(work_dir or (Path(tempfile.gettempdir()) / "Planet_Downloader" / uuid.uuid4().hex))
    upload_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()
    state = {"id": None, "status": "idle", "logs": [], "plan": None, "summary": None,
             "error": None, "api_key": "", "job_dir": None, "cancel": threading.Event()}
    vectors = {}
    app.extensions.update(planet_state=state, planet_lock=lock, planet_token=token)

    def active():
        return state["status"] in ("searching", "running")

    def emit(message):
        text = safe_error(str(message), state.get("api_key", ""))
        with lock:
            state["logs"].append(f"{datetime.now().strftime('%H:%M:%S')}  {text}")
            state["logs"] = state["logs"][-2000:]

    def require_key(value):
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 256 or any(c.isspace() for c in value.strip()):
            raise ValueError("Inserisci una chiave API Planet valida.")
        return value.strip()

    def worker(fn):
        def run():
            try:
                fn()
            except InterruptedError:
                with lock:
                    state["status"] = "paused"
                emit("Lavoro locale interrotto. Gli ordini già inviati a Planet possono continuare. Per riprendere carica questa cartella di lavoro.")
            except Exception as exc:
                error = safe_error(exc, state.get("api_key", ""))
                with lock:
                    state.update(status="error", error=error)
                emit(error)
        thread = threading.Thread(target=run, daemon=True, name="planet-worker")
        thread.start()

    @app.before_request
    def local_only():
        host = request.host.split(":", 1)[0].lower()
        if host not in ("localhost", "127.0.0.1"):
            return jsonify(error="Host non consentito."), 403
        origin = request.headers.get("Origin")
        if origin and origin not in (f"http://{request.host}", f"https://{request.host}"):
            return jsonify(error="Origine non consentita."), 403
        if request.path.startswith("/api/") and not secrets.compare_digest(request.headers.get("X-App-Token", ""), token):
            return jsonify(error="Sessione GUI non valida: ricarica la pagina."), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=safe_error(exc, state.get("api_key", ""))), 400

    @app.errorhandler(413)
    def large(_):
        return jsonify(error="File troppo grande: massimo 256 MB per caricamento."), 413

    @app.errorhandler(500)
    def internal(_):
        return jsonify(error="Errore locale del programma. Controlla la finestra di avvio e riprova."), 500

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=token)

    @app.get("/api/info")
    def info():
        from .catalog import PRODUCTS
        return jsonify(products=PRODUCTS, defaults=DEFAULTS, version="1.0.0")

    @app.post("/api/vector")
    def vector():
        from .geometry import load_areas, list_layers
        with lock:
            if active():
                return jsonify(error="Attendi o interrompi il lavoro in corso."), 409
        layer = request.form.get("layer", "").strip()
        name_field = request.form.get("name_field", "").strip()
        existing = request.form.get("vector_id", "")
        if existing and existing in vectors:
            path = vectors[existing]["path"]
            vector_id = existing
        else:
            files = request.files.getlist("files") or request.files.getlist("files[]")
            if not files:
                raise ValueError("Carica un file vettoriale o tutti i componenti dello shapefile.")
            vector_id = uuid.uuid4().hex
            folder = upload_dir / vector_id
            folder.mkdir()
            for file in files:
                name = Path(file.filename.replace("\\", "/")).name
                if not name or Path(name).suffix.lower() not in VECTOR_EXTS | {".zip"}:
                    raise ValueError("Formato non supportato: usa GeoPackage, GeoJSON, shapefile o ZIP contenente uno shapefile.")
                target = folder / name
                if target.exists():
                    raise ValueError("Due file caricati hanno lo stesso nome.")
                file.save(target)
                if target.suffix.lower() == ".zip":
                    with zipfile.ZipFile(target) as archive:
                        members = [m for m in archive.infolist() if not m.is_dir() and "__MACOSX" not in m.filename]
                        if sum(m.file_size for m in members) > MAX_UPLOAD or len(members) > 200:
                            raise ValueError("Archivio vettoriale troppo grande o con troppi file.")
                        for member in members:
                            relative = PurePosixPath(member.filename.replace("\\", "/"))
                            if relative.is_absolute() or ".." in relative.parts or any(":" in x for x in relative.parts):
                                raise ValueError("Percorso non valido nell'archivio ZIP.")
                            if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                                raise ValueError("Collegamenti simbolici non ammessi nello ZIP.")
                            if relative.suffix.lower() not in VECTOR_EXTS:
                                continue
                            dest = folder / "estratto" / Path(*relative.parts)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if dest.exists():
                                raise ValueError("Nomi duplicati nell'archivio.")
                            with archive.open(member) as src, dest.open("wb") as dst:
                                count = 0
                                while chunk := src.read(1024 * 1024):
                                    count += len(chunk)
                                    if count > MAX_UPLOAD:
                                        raise ValueError("File decompresso troppo grande.")
                                    dst.write(chunk)
            choices = [p for p in folder.rglob("*") if p.suffix.lower() in {".shp", ".gpkg", ".geojson", ".json"}]
            if len(choices) != 1:
                raise ValueError("Carica un solo dataset: un GeoPackage/GeoJSON oppure un solo shapefile con i suoi componenti.")
            path = choices[0]
        layers = list_layers(str(path))
        areas = load_areas(str(path), layer=layer, name_field=name_field)
        vectors[vector_id] = {"path": path, "areas": areas, "layer": layer, "name_field": name_field}
        return jsonify(vector_id=vector_id, areas=[{k: a[k] for k in ("id", "name", "area_km2")} for a in areas], layers=layers)

    @app.post("/api/browse")
    def browse():
        # A separate process keeps Tk native dialogs on their own main thread.
        result = subprocess.run([sys.executable, "-m", "planet_downloader.picker"], capture_output=True, text=True, timeout=180)
        if result.returncode:
            return jsonify(error="Selettore cartelle non disponibile: incolla il percorso completo nel campo."), 400
        return jsonify(path=result.stdout.strip())

    @app.post("/api/search")
    def search():
        from .api import PlanetClient
        from .catalog import build_plan
        data = request.get_json(silent=True) or {}
        config = validate_config(data.get("config", {}))
        key = require_key(data.get("api_key"))
        selected = vectors.get(data.get("vector_id", ""))
        if selected is None:
            raise ValueError("Carica prima il vettoriale delle aree di studio.")
        if (config["layer"], config["name_field"]) != (selected["layer"], selected["name_field"]):
            raise ValueError("Layer o campo nome modificati: ricarica il vettoriale con queste opzioni prima della ricerca.")
        with lock:
            if active():
                return jsonify(error="Un lavoro è già in corso."), 409
            job_id = uuid.uuid4().hex
            job_dir = Path(config["output_dir"]) / f"Planet_job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{job_id[:6]}"
            job_dir.mkdir(parents=True)
            state.update(id=job_id, status="searching", logs=[], plan=None, summary=None, error=None,
                         api_key=key, job_dir=str(job_dir), cancel=threading.Event())
        def run_search():
            emit("Ricerca nel catalogo Planet. Nessun ordine di immagini sarà inviato prima della conferma.")
            if config["aoi_cloud_max"] is not None or config["aoi_clear_min"] is not None:
                emit("Il controllo qualità sull'area può attivare le maschere UDM2 presso Planet.")
            client = PlanetClient(key, emit=emit, cancel=state["cancel"])
            plan = build_plan(client, selected["areas"], config, emit, state["cancel"])
            plan["job_id"] = job_id
            atomic_json(job_dir / "plan.json", plan)
            (job_dir / "plan.sha256").write_text(hashlib.sha256((job_dir / "plan.json").read_bytes()).hexdigest(), encoding="ascii")
            with lock:
                state.update(plan=plan, status="ready")
            emit("Ricerca conclusa. Controlla il numero di immagini e la stima dei km², poi conferma il download.")
        worker(run_search)
        return jsonify(job_id=job_id)

    @app.get("/api/job")
    def job():
        with lock:
            return jsonify({**{k: state[k] for k in ("id", "status", "logs", "summary", "error", "job_dir")}, "plan": compact_plan(state["plan"])})

    @app.post("/api/confirm")
    def confirm():
        from .api import PlanetClient
        from .workflow import process_plan
        data = request.get_json(silent=True) or {}
        with lock:
            if data.get("confirmed") is not True:
                raise ValueError("È necessaria la conferma esplicita dell'ordine.")
            if data.get("job_id") != state["id"] or state["status"] != "ready":
                return jsonify(error="Ricerca non pronta o già avviata. Ricarica lo stato."), 409
            plan = state["plan"]
            if not any(a.get("orders") for a in plan["areas"]):
                raise ValueError("Nessuna immagine ordinabile con i filtri impostati.")
            key, job_dir = state["api_key"], Path(state["job_dir"])
            atomic_json(job_dir / "confirmation.json", {"confirmed_at": datetime.now(timezone.utc).isoformat(), "job_id": state["id"], "totals": plan["totals"]})
            state.update(status="running", error=None, cancel=threading.Event())
        def run_download():
            client = PlanetClient(key, emit=emit, cancel=state["cancel"])
            summary = process_plan(client, plan, job_dir, emit, state["cancel"])
            atomic_json(job_dir / "summary.json", summary)
            with lock:
                state.update(status="done", summary=summary, api_key="")
            emit("Lavoro concluso: controlla il riepilogo e le eventuali immagini mancanti o escluse.")
        worker(run_download)
        return jsonify(job_id=state["id"])

    @app.post("/api/cancel")
    def cancel():
        with lock:
            if active():
                state["cancel"].set()
        return jsonify(ok=True)

    @app.post("/api/resume")
    def resume():
        data = request.get_json(silent=True) or {}
        key = require_key(data.get("api_key"))
        folder = Path(str(data.get("job_dir", ""))).expanduser()
        if not folder.is_absolute():
            raise ValueError("Inserisci il percorso completo della cartella Planet_job da riprendere.")
        folder = folder.resolve()
        source = folder / "plan.json"
        if not source.is_file() or source.stat().st_size > MAX_UPLOAD:
            raise ValueError("La cartella non contiene un piano valido (plan.json).")
        digest_file = folder / "plan.sha256"
        if not digest_file.exists() or digest_file.read_text().strip() != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError("Il piano è stato modificato o è incompleto. Avvia una nuova ricerca.")
        plan = json.loads(source.read_text(encoding="utf-8"))
        if plan.get("version") != 1 or not isinstance(plan.get("areas"), list):
            raise ValueError("Versione del piano non supportata.")
        validate_config(plan.get("config", {}))
        with lock:
            if active():
                return jsonify(error="Interrompi prima il lavoro in corso."), 409
            state.update(id=uuid.uuid4().hex, status="ready", logs=[], plan=plan, summary=None,
                         error=None, api_key=key, job_dir=str(folder), cancel=threading.Event())
        emit("Piano caricato. La conferma riprenderà ordini e file esistenti dalla cartella selezionata.")
        return jsonify(job_id=state["id"])

    @app.get("/api/plan")
    def export_plan():
        if not state["plan"]:
            raise ValueError("Nessun piano disponibile.")
        return Response(json.dumps(state["plan"], ensure_ascii=False, indent=2), mimetype="application/json", headers={"Content-Disposition": 'attachment; filename="piano_planet.json"'})

    @app.get("/api/report")
    def export_csv():
        if not state["plan"]:
            raise ValueError("Nessuna ricerca disponibile.")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["area", "scene_id", "acquired_UTC", "cloud_scene_percent", "clear_scene_percent", "cloud_AOI_percent", "clear_AOI_percent", "geometric_coverage_percent", "clipped_km2"])
        for area in state["plan"]["areas"]:
            for scene in area["scenes"]:
                row = [area["name"], scene["id"], scene["acquired"], scene.get("cloud_percent"), scene.get("clear_percent"), scene.get("aoi_cloud_percent"), scene.get("aoi_clear_percent"), scene.get("coverage_percent"), scene.get("clipped_km2")]
                writer.writerow([("'" + v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in row])
        return Response("\ufeff" + output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="scene_planet.csv"'})

    return app
