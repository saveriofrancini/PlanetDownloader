/* Local UI only. Credentials never enter localStorage, sessionStorage or exported settings. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const token = document.querySelector('meta[name="csrf-token"]').content;
  const state = { ready: false, pending: false, vectorId: null, vectorSignature: "", products: {},
    job: null, plan: null, planKey: "", stale: false, poll: null, polling: false,
    rows: [], shown: 0, applyResumedConfig: false, connectionErrors: 0 };
  const configInputs = {
    product: "product", start_date: "start-date", end_date: "end-date", cloud_scene_max: "cloud-scene-max",
    aoi_cloud_max: "aoi-cloud-max", aoi_clear_min: "aoi-clear-min", coverage_min: "coverage-min",
    quality_standard: "quality-standard", ground_control: "ground-control", sun_elevation_min: "sun-elevation-min",
    view_angle_max: "view-angle-max", output_dir: "output-dir", target_epsg: "target-epsg", resolution: "resolution",
    clear_only: "clear-only", confidence_min: "confidence-min", batch_size: "batch-size", name_field: "name-field", layer: "layer"
  };
  const numericKeys = new Set(["cloud_scene_max", "aoi_cloud_max", "aoi_clear_min", "coverage_min", "sun_elevation_min", "view_angle_max", "resolution", "confidence_min", "batch_size"]);
  const booleanKeys = new Set(["quality_standard", "ground_control", "clear_only"]);
  const activeStatuses = new Set(["searching", "running"]);
  const fmt = (value, decimals = 0) => Number.isFinite(Number(value)) && value !== null && value !== "" ? Number(value).toLocaleString("it-IT", { maximumFractionDigits: decimals }) : "—";
  const percent = (value) => value === null || value === undefined || value === "" ? "—" : `${fmt(value, 1)}%`;
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = String(text); if (className) el.className = className; return el; };
  const isActive = () => state.job && activeStatuses.has(state.job.status);
  const isPartial = (summary) => !!summary && (summary.status === "partial" || summary.status === "error" || summary.partial === true || (Array.isArray(summary.failed) ? summary.failed.length > 0 : Number(summary.failed) > 0) || Number(summary.failed_orders) > 0 || Number(summary.excluded_scenes) > 0);

  async function api(path, options = {}) {
    const headers = { "X-App-Token": token, ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    let response;
    try { response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" }); }
    catch (_) { throw new Error("Non riesco a contattare il programma locale. Verifica che la finestra di avvio sia ancora aperta."); }
    if (!response.ok) {
      let message = `Richiesta non riuscita (HTTP ${response.status}).`;
      try { const payload = await response.json(); message = payload.error || payload.message || message; } catch (_) { /* Keep a generic message; never expose a raw server page. */ }
      throw new Error(typeof message === "string" ? message : JSON.stringify(message));
    }
    return options.blob ? response.blob() : response.json();
  }

  function error(message) {
    $("global-error").textContent = message || "";
    $("global-error").hidden = !message;
    if (message) $("global-error").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function conditionalControls() {
    const locked = state.pending || isActive();
    $("aoi-cloud-max").disabled = locked || !$("aoi-cloud-enabled").checked;
    $("aoi-clear-min").disabled = locked || !$("aoi-clear-enabled").checked;
    $("mask-custom").hidden = $("clear-only").checked;
    document.querySelectorAll('[name="mask_class"]').forEach((el) => { el.disabled = locked || $("clear-only").checked; });
  }

  function controls() {
    const locked = state.pending || !!isActive();
    document.querySelectorAll("#config-form input, #config-form select, #config-form button").forEach((el) => { el.disabled = locked; });
    $("search-button").disabled = !state.ready || locked;
    $("search-button").textContent = state.job?.status === "searching" ? "Ricerca in corso…" : "Cerca immagini →";
    $("resume-button").disabled = !state.ready || locked;
    $("resume-dir").disabled = locked;
    $("confirm-button").disabled = locked || state.stale || !state.plan || state.job?.status !== "ready" || !(Number(state.plan.totals?.scene_count) > 0);
    $("cancel-button").hidden = !isActive();
    $("cancel-button").disabled = state.pending;
    $("cancel-note").hidden = !isActive();
    $("stale-notice").hidden = !state.stale;
    conditionalControls();
  }

  function markDirty() {
    if (state.plan) state.stale = true;
    controls();
  }

  function readConfig() {
    const config = {};
    for (const [key, id] of Object.entries(configInputs)) {
      if (booleanKeys.has(key)) config[key] = $(id).checked;
      else if (numericKeys.has(key)) config[key] = Number($(id).value);
      else config[key] = $(id).value.trim();
    }
    if (!$("aoi-cloud-enabled").checked) config.aoi_cloud_max = null;
    if (!$("aoi-clear-enabled").checked) config.aoi_clear_min = null;
    config.mask_classes = [...document.querySelectorAll('[name="mask_class"]:checked')].map((el) => el.value);
    return config;
  }

  function applyConfig(config) {
    if (!config || typeof config !== "object" || Array.isArray(config)) throw new Error("Il file non contiene un oggetto di impostazioni valido.");
    for (const [key, id] of Object.entries(configInputs)) {
      if (!Object.prototype.hasOwnProperty.call(config, key)) continue;
      const value = config[key];
      if (booleanKeys.has(key)) { if (typeof value === "boolean") $(id).checked = value; }
      else if (numericKeys.has(key)) { if (value !== null && Number.isFinite(Number(value))) $(id).value = value; }
      else if (typeof value === "string") { if (key !== "product" || state.products[value]) $(id).value = value; }
    }
    if (Object.prototype.hasOwnProperty.call(config, "aoi_cloud_max")) $("aoi-cloud-enabled").checked = config.aoi_cloud_max !== null;
    if (Object.prototype.hasOwnProperty.call(config, "aoi_clear_min")) $("aoi-clear-enabled").checked = config.aoi_clear_min !== null;
    if (Array.isArray(config.mask_classes)) document.querySelectorAll('[name="mask_class"]').forEach((el) => { el.checked = config.mask_classes.includes(el.value); });
    productInfo();
    conditionalControls();
  }

  function productInfo() {
    const product = state.products[$("product").value];
    $("product-description").textContent = product ? `${product.item_type} · ${product.bands} bande · pixel nativo indicativo ${fmt(product.resolution, 1)} m. Disponibilità e permessi dipendono dall’account.` : "";
  }

  function vectorSignature() {
    return JSON.stringify({ files: [...$("vector-files").files].map((f) => [f.name, f.size, f.lastModified]), layer: $("layer").value.trim(), name_field: $("name-field").value.trim() });
  }

  async function loadVector() {
    const files = [...$("vector-files").files];
    if (!files.length) throw new Error("Seleziona il file vettoriale. Per uno Shapefile seleziona insieme almeno .shp, .shx, .dbf e .prj oppure carica uno ZIP.");
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    form.append("layer", $("layer").value.trim());
    form.append("name_field", $("name-field").value.trim());
    $("vector-status").classList.remove("is-error");
    $("vector-status").textContent = "Lettura del file e controllo delle geometrie…";
    const data = await api("/api/vector", { method: "POST", body: form });
    state.vectorId = data.vector_id;
    state.vectorSignature = vectorSignature();
    const areas = data.areas || [];
    const km2 = areas.reduce((sum, area) => sum + Number(area.area_km2 || 0), 0);
    $("vector-status").textContent = `${fmt(areas.length)} ${areas.length === 1 ? "area caricata" : "aree caricate"} · ${fmt(km2, 3)} km² complessivi`;
    $("area-list").replaceChildren();
    areas.forEach((area) => { const row = node("div", undefined, "area-item"); row.append(node("span", area.name || area.id), node("span", `${fmt(area.area_km2, 3)} km²`)); $("area-list").append(row); });
    $("area-list").hidden = !areas.length;
    $("vector-layers").replaceChildren();
    (data.layers || []).forEach((layer) => { const option = document.createElement("option"); option.value = typeof layer === "string" ? layer : layer.name || ""; $("vector-layers").append(option); });
    return data;
  }

  async function action(fn) {
    if (state.pending) return;
    error(""); state.pending = true; controls();
    try { await fn(); }
    catch (err) { error(err.message || "Operazione non riuscita."); }
    finally { state.pending = false; controls(); }
  }

  function requireKey() {
    const key = $("api-key").value.trim();
    if (!key) throw new Error("Inserisci la chiave API Planet prima di continuare.");
    return key;
  }

  function startPolling() {
    if (state.poll) clearTimeout(state.poll);
    state.poll = setTimeout(pollJob, 500);
  }

  async function pollJob() {
    if (state.polling) return;
    state.polling = true;
    try {
      const job = await api("/api/job");
      state.connectionErrors = 0;
      if (job && job.status !== "idle") renderJob(job);
    } catch (err) {
      state.connectionErrors += 1;
      $("job-error").hidden = false;
      $("job-error").textContent = `${err.message} Riprovo automaticamente.`;
    } finally {
      state.polling = false;
      if (isActive() || state.pending || state.connectionErrors > 0) state.poll = setTimeout(pollJob, state.connectionErrors ? 5000 : 2000);
    }
  }

  function renderJob(job) {
    const oldStatus = state.job?.status;
    state.job = job;
    $("job-panel").hidden = false;
    const statuses = { searching: "Ricerca", ready: "Da confermare", running: "In corso", done: "Terminato", error: "Errore", paused: "Interrotto" };
    const descriptions = {
      searching: "Ricerca delle scene, controlli UDM2 e preparazione degli ordini. Nessun ordine viene inviato in questa fase.",
      ready: "Il piano è disponibile. Controlla immagini e stima prima di confermare.",
      running: "Ordini, download e mosaici in elaborazione. Mantieni aperto il programma locale.",
      done: "L’elaborazione è terminata. Consulta l’esito per verificare immagini escluse o eventuali problemi.",
      error: "Il lavoro non è stato completato. Consulta il messaggio e il registro; se una cartella di lavoro è già stata creata puoi riprenderla.",
      paused: "Il lavoro locale è stato interrotto. Gli ordini già inviati a Planet possono essere ancora in esecuzione."
    };
    const partial = isPartial(job.summary);
    $("job-status").textContent = partial && job.status === "done" ? "Parziale" : statuses[job.status] || job.status;
    $("job-status").className = `status-chip ${job.status === "error" ? "error" : job.status === "paused" || partial ? "paused" : ""}`;
    $("job-id").textContent = job.id ? `Lavoro ${job.id}` : "";
    $("job-description").textContent = descriptions[job.status] || "";
    $("job-location").textContent = job.job_dir ? `Cartella del lavoro:\n${job.job_dir}` : "";
    $("job-location").hidden = !job.job_dir;
    if (job.job_dir) $("resume-dir").value = job.job_dir;
    $("job-spinner").hidden = !activeStatuses.has(job.status);
    $("job-error").textContent = job.error || "";
    $("job-error").hidden = !job.error;
    const logLines = Array.isArray(job.logs) ? job.logs : [];
    const logText = logLines.slice(-400).map((line) => typeof line === "string" ? line : JSON.stringify(line)).join("\n");
    const logBox = $("job-logs");
    if (logBox.textContent !== logText) {
      const atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 45;
      logBox.textContent = logText || "In attesa delle attività…";
      if (atBottom) logBox.scrollTop = logBox.scrollHeight;
    }
    [1, 2, 3].forEach((step) => $("step-" + step).classList.toggle("current", step === (job.status === "ready" ? 2 : ["running", "done", "paused"].includes(job.status) ? 3 : 1)));
    if (job.plan) {
      const planKey = `${job.id}|${job.plan.created_at}|${JSON.stringify(job.plan.totals)}`;
      if (planKey !== state.planKey) {
        state.planKey = planKey; state.plan = job.plan; state.stale = false;
        if (state.applyResumedConfig) { applyConfig(job.plan.config); state.applyResumedConfig = false; }
        renderPlan(job.plan);
        if (job.status === "ready") $("results").scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
    const downloadStarted = ["running", "done", "paused", "error"].includes(job.status);
    $("confirm-title").textContent = downloadStarted ? "Piano del lavoro" : "Pronto per il download?";
    $("confirm-description").textContent = downloadStarted ? "Gli ordini e le elaborazioni seguono questo piano. Verifica lo stato del lavoro e il riepilogo dell’esito." : "Saranno scaricate tutte le immagini del piano. I file finali sono organizzati per area e data.";
    if (job.summary) renderCompletion(job.summary, job.status);
    else if (oldStatus !== job.status && activeStatuses.has(job.status)) $("completion").hidden = true;
    controls();
  }

  function renderWarnings(container, warnings) {
    container.replaceChildren();
    const lines = Array.isArray(warnings) ? warnings : warnings ? [warnings] : [];
    lines.forEach((warning) => container.append(node("p", typeof warning === "string" ? warning : JSON.stringify(warning))));
    container.hidden = !lines.length;
  }

  function renderPlan(plan) {
    const totals = plan.totals || {};
    const config = plan.config || {};
    $("results").hidden = false;
    const product = state.products[config.product]?.label || config.product || "";
    $("plan-context").textContent = `${product} · ${config.start_date || ""} → ${config.end_date || ""} · Date UTC incluse`;
    $("metric-scenes").textContent = fmt(totals.scene_count);
    $("metric-areas").textContent = fmt(totals.area_count);
    $("metric-km2").textContent = fmt(totals.estimated_km2, 3);
    $("metric-orders").textContent = fmt(totals.order_count);
    $("unique-scenes").textContent = `${fmt(totals.unique_scene_count)} scene uniche`;
    $("day-count").textContent = `${fmt(totals.day_count)} combinazioni area / giorno`;
    $("estimate-details").textContent = `Area ritagliata senza ripetizioni degli ordini: ${fmt(totals.unique_clipped_km2, 3)} km². Confronto con le scene intere: ${fmt(totals.full_scene_km2, 3)} km².`;
    const warnings = [...(Array.isArray(plan.warnings) ? plan.warnings : [])];
    $("areas-summary").replaceChildren();
    state.rows = []; state.shown = 0;
    (plan.areas || []).forEach((area) => {
      const scenes = area.scenes || [];
      const dates = new Set(scenes.map((scene) => scene.date || (scene.acquired || "").slice(0, 10)));
      const areaCard = node("div", undefined, "area-summary-item");
      areaCard.append(node("h3", area.name || area.id), node("p", `${fmt(area.area_km2, 3)} km² · ${fmt(scenes.length)} scene · ${fmt(dates.size)} giorni · ${fmt((area.zones || []).length)} zone di coregistrazione`));
      $("areas-summary").append(areaCard);
      (area.warnings || []).forEach((warning) => warnings.push(`${area.name || area.id}: ${typeof warning === "string" ? warning : JSON.stringify(warning)}`));
      scenes.forEach((scene) => state.rows.push({ area: area.name || area.id, ...scene }));
    });
    if (!Number(totals.scene_count)) warnings.unshift("Nessuna immagine rispetta i filtri. Amplia il periodo o modifica le soglie, poi ripeti la ricerca.");
    renderWarnings($("plan-warnings"), [...new Set(warnings)]);
    $("scene-table-label").textContent = `Elenco delle immagini · ${fmt(state.rows.length)} scene × aree`;
    $("scenes-table").replaceChildren();
    appendRows();
  }

  function appendRows() {
    const end = Math.min(state.shown + 200, state.rows.length);
    const fragment = document.createDocumentFragment();
    for (let i = state.shown; i < end; i += 1) {
      const scene = state.rows[i]; const row = document.createElement("tr");
      [scene.area, scene.date || (scene.acquired || "").slice(0, 10), scene.id, percent(scene.cloud_percent), percent(scene.clear_percent), percent(scene.aoi_cloud_percent), percent(scene.aoi_clear_percent), percent(scene.coverage_percent)].forEach((value) => row.append(node("td", value)));
      fragment.append(row);
    }
    $("scenes-table").append(fragment); state.shown = end;
    $("more-scenes").hidden = end >= state.rows.length;
    $("more-scenes").textContent = `Mostra altre ${Math.min(200, state.rows.length - end)} righe (${fmt(end)} di ${fmt(state.rows.length)})`;
  }

  function renderCompletion(summary, status) {
    $("completion").hidden = false;
    const warnings = [...(Array.isArray(summary.warnings) ? summary.warnings : [])];
    const incomplete = status !== "done" || isPartial(summary);
    $("completion-title").textContent = incomplete ? "Lavoro terminato con elementi da verificare" : "Elaborazione terminata";
    $("completion-description").textContent = incomplete ? "Alcune immagini o elaborazioni non sono state completate. Controlla i dettagli prima di utilizzare i mosaici." : "Controlla il riepilogo dei file prodotti e delle eventuali esclusioni prima di utilizzare i dati.";
    const directory = summary.job_dir || summary.output_dir || "";
    $("completion-location").textContent = directory;
    $("completion-location").hidden = !directory;
    if (directory) $("resume-dir").value = directory;
    renderWarnings($("completion-warnings"), warnings);
    $("completion-json").textContent = JSON.stringify(summary, null, 2);
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob); const anchor = document.createElement("a");
    anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  $("config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!$("config-form").reportValidity()) return;
    action(async () => {
      const apiKey = requireKey(); const config = readConfig();
      if (config.start_date > config.end_date) throw new Error("La data iniziale deve precedere o coincidere con la data finale.");
      if (!state.vectorId || state.vectorSignature !== vectorSignature()) await loadVector();
      const data = await api("/api/search", { method: "POST", body: { api_key: apiKey, vector_id: state.vectorId, config } });
      state.plan = null; state.planKey = ""; state.stale = false;
      $("results").hidden = true; $("completion").hidden = true;
      renderJob({ id: data.job_id, status: "searching", logs: [] }); startPolling();
    });
  });

  $("load-vector").addEventListener("click", () => action(async () => { markDirty(); await loadVector(); }));
  $("vector-files").addEventListener("change", () => { state.vectorId = null; $("area-list").hidden = true; $("vector-status").textContent = "Premi “Leggi le aree” per controllare il file."; markDirty(); });
  $("config-form").addEventListener("input", (event) => { if (!["api-key", "settings-file"].includes(event.target.id)) markDirty(); });
  $("config-form").addEventListener("change", (event) => { if (!["api-key", "settings-file"].includes(event.target.id)) markDirty(); });
  $("product").addEventListener("change", productInfo);
  $("toggle-key").addEventListener("click", () => { const shown = $("api-key").type === "password"; $("api-key").type = shown ? "text" : "password"; $("toggle-key").textContent = shown ? "Nascondi" : "Mostra"; $("toggle-key").setAttribute("aria-pressed", String(shown)); $("toggle-key").setAttribute("aria-label", shown ? "Nascondi chiave API" : "Mostra chiave API"); });

  $("browse-output").addEventListener("click", () => action(async () => {
    try { const data = await api("/api/browse", { method: "POST", body: { kind: "output" } }); if (data.path) { $("output-dir").value = data.path; markDirty(); } }
    catch (_) { throw new Error("La finestra di scelta cartella non è disponibile. Copia e incolla il percorso completo in “Cartella dei download”."); }
  }));

  $("confirm-button").addEventListener("click", () => {
    if (!state.plan || state.stale || state.job?.status !== "ready") return;
    const totals = state.plan.totals;
    const message = `Confermi l’invio degli ordini Planet e il download?\n\n${fmt(totals.scene_count)} scene × aree (${fmt(totals.unique_scene_count)} scene uniche).\nStima geometrica: ${fmt(totals.estimated_km2, 3)} km², inclusi i riferimenti ripetuti.\n\nIl consumo effettivo dipende dal tuo contratto Planet. Gli ordini già inviati possono proseguire anche se interrompi il lavoro locale.`;
    if (!window.confirm(message)) return;
    action(async () => { await api("/api/confirm", { method: "POST", body: { job_id: state.job.id, confirmed: true } }); renderJob({ ...state.job, status: "running" }); startPolling(); });
  });

  $("cancel-button").addEventListener("click", () => action(async () => {
    await api("/api/cancel", { method: "POST", body: {} });
    $("job-description").textContent = "Interruzione richiesta. Attendi che l’operazione corrente termini in sicurezza.";
    startPolling();
  }));

  $("resume-button").addEventListener("click", () => action(async () => {
    const apiKey = requireKey(); const directory = $("resume-dir").value.trim();
    if (!directory) throw new Error("Inserisci il percorso della cartella Planet_job_… da riprendere.");
    const data = await api("/api/resume", { method: "POST", body: { api_key: apiKey, job_dir: directory } });
    state.planKey = ""; state.plan = null; state.stale = false; state.applyResumedConfig = true;
    $("results").hidden = true; $("completion").hidden = true;
    renderJob({ id: data.job_id, status: "ready", logs: ["Caricamento del piano salvato…"] }); startPolling();
  }));

  $("more-scenes").addEventListener("click", appendRows);
  $("export-csv").addEventListener("click", () => action(async () => saveBlob(await api("/api/report", { blob: true }), "Planet_scene_pianificate.csv")));
  $("export-plan").addEventListener("click", () => action(async () => saveBlob(await api("/api/plan", { blob: true }), "Planet_piano_download.json")));
  $("save-settings").addEventListener("click", () => {
    const data = { version: 1, saved_at: new Date().toISOString(), config: readConfig() };
    saveBlob(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }), "Planet_impostazioni.json");
  });
  $("load-settings").addEventListener("click", () => $("settings-file").click());
  $("settings-file").addEventListener("change", () => action(async () => {
    const file = $("settings-file").files[0]; if (!file) return;
    if (file.size > 1024 * 1024) throw new Error("Il file di impostazioni è troppo grande. Carica un JSON delle impostazioni, non il piano completo.");
    let data; try { data = JSON.parse(await file.text()); } catch (_) { throw new Error("Il file selezionato non contiene JSON valido."); }
    applyConfig(data.config || data); state.vectorId = null; markDirty();
    $("settings-file").value = "";
    $("vector-status").textContent = "Impostazioni caricate. Seleziona o rileggi il file vettoriale; la chiave API non viene caricata dal file.";
  }));

  async function init() {
    try {
      const data = await api("/api/info"); state.products = data.products || {};
      Object.entries(state.products).forEach(([id, product]) => { const option = document.createElement("option"); option.value = id; option.textContent = product.label || id; $("product").append(option); });
      applyConfig(data.defaults || {}); state.ready = true; $("boot-message").hidden = true;
      const existing = await api("/api/job");
      if (existing && existing.status !== "idle") { state.applyResumedConfig = true; renderJob(existing); if (isActive()) startPolling(); }
    } catch (err) { $("boot-message").hidden = true; error(err.message); }
    controls();
  }
  init();
})();
