"""Windowed local mosaics of Planet scenes already registered to zone anchors.

The imagery is masked on its own native grid *before* any interpolation.  A
nearest-neighbour quality raster then gates the resampled imagery as well.
All dates of one area use the same, globally snapped, metric pixel grid.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.errors import WindowError
from rasterio.features import geometry_mask, geometry_window
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window, bounds as window_bounds, transform as window_transform
from shapely.geometry import mapping, shape
from shapely.ops import transform as geometry_transform, unary_union

NODATA = -9999.0
SCORE_NODATA = 65535
BLOCK_SIZE = 512
MASK_BANDS = {"snow": 2, "shadow": 3, "light_haze": 4, "heavy_haze": 5, "cloud": 6}


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Elaborazione raster interrotta; gli originali sono conservati.")


def _write_json(path: Path, value: Any):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(temporary, path)


def _geometry(value):
    geom = shape(value)
    if geom.is_empty or not geom.is_valid or geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Geometria AOI/zona vuota, non valida o non poligonale.")
    return geom


def _polygonal(geom):
    """Discard boundary-only lines/points returned by a polygon intersection."""
    if geom.geom_type in {"Polygon", "MultiPolygon"}:
        return geom
    polygons = []
    for part in getattr(geom, "geoms", []):
        cleaned = _polygonal(part)
        if not cleaned.is_empty:
            polygons.append(cleaned)
    return unary_union(polygons)


def _project(geom, src, dst):
    source = CRS.from_user_input(src)
    target = CRS.from_user_input(dst)
    if source == target:
        return geom
    # Densification retains curved projected boundaries of large geographic AOIs.
    if source.is_geographic:
        geom = geom.segmentize(0.025)
    transformer = Transformer.from_crs(source, target, always_xy=True)
    return geometry_transform(transformer.transform, geom)


def _make_grid(area_geometry, config):
    resolution = float(config.get("resolution", 3.0))
    if not math.isfinite(resolution) or resolution <= 0:
        raise ValueError("La risoluzione deve essere un numero positivo in metri.")
    requested = str(config.get("target_epsg") or "").strip()
    if requested:
        crs = CRS.from_user_input(requested if ":" in requested else f"EPSG:{requested}")
    else:
        center = area_geometry.centroid
        if not (-80 <= center.y <= 84):
            raise ValueError("Fuori dai limiti UTM: scegliere un EPSG proiettato in metri.")
        zone = max(1, min(60, math.floor((center.x + 180) / 6) + 1))
        crs = CRS.from_epsg((32600 if center.y >= 0 else 32700) + zone)
    if not crs.is_projected or not crs.axis_info or any(
        abs(axis.unit_conversion_factor - 1.0) > 1e-10 for axis in crs.axis_info[:2]
    ):
        raise ValueError("Il CRS di uscita deve essere proiettato e usare metri.")
    projected = _project(area_geometry, "EPSG:4326", crs)
    left, bottom, right, top = projected.bounds
    if not all(math.isfinite(v) for v in projected.bounds):
        raise ValueError("Impossibile proiettare l'area nel CRS scelto.")
    # Suppress sub-micrometre round-trip noise near an exact grid boundary.
    epsilon = 1e-7
    left = math.floor(left / resolution + epsilon) * resolution
    bottom = math.floor(bottom / resolution + epsilon) * resolution
    right = math.ceil(right / resolution - epsilon) * resolution
    top = math.ceil(top / resolution - epsilon) * resolution
    width = int(round((right - left) / resolution))
    height = int(round((top - bottom) / resolution))
    if not 0 < width < 2**31 or not 0 < height < 2**31:
        raise ValueError("Dimensioni della griglia non supportate; suddividere l'area.")
    return {
        "crs": rasterio.crs.CRS.from_wkt(crs.to_wkt()),
        "transform": from_origin(left, top, resolution, resolution),
        "width": width, "height": height, "geometry": projected,
        "resolution": resolution,
    }


def _grid_metadata(grid):
    return {
        "epsg": grid["crs"].to_epsg(), "crs_wkt": grid["crs"].to_wkt(),
        "transform": list(grid["transform"])[:6], "width": grid["width"],
        "height": grid["height"], "resolution_m": grid["resolution"],
        "pixel_selection": "pixel center inside AOI and registration zone; polygon holes excluded",
    }


def _profile(grid, count, dtype, nodata):
    return dict(
        driver="GTiff", width=grid["width"], height=grid["height"], count=count,
        dtype=dtype, crs=grid["crs"], transform=grid["transform"], nodata=nodata,
        tiled=True, blockxsize=BLOCK_SIZE, blockysize=BLOCK_SIZE,
        compress="deflate", predictor=3 if dtype == "float32" else 2,
        BIGTIFF="IF_SAFER", num_threads="2",
    )


def _windows(width, height):
    for row in range(0, height, BLOCK_SIZE):
        for col in range(0, width, BLOCK_SIZE):
            yield Window(col, row, min(BLOCK_SIZE, width - col), min(BLOCK_SIZE, height - row))


def _date(record):
    acquired = record.get("acquired")
    if acquired:
        value = datetime.fromisoformat(str(acquired).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat()
    value = str(record.get("date", ""))
    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def _number(value, default):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _sort_key(record):
    # Equal per-pixel scores retain the earlier record in this deterministic order.
    return (
        -_number(record.get("clear_percent"), -1),
        _number(record.get("cloud_percent"), 101),
        str(record["scene_id"]), str(record["zone_id"]), str(record["image_path"]),
    )


def _prepare_source(record, zone_wgs84, config, tempdir: Path, sequence, cancel):
    """Clean a cropped native scene on disk; never interpolate contaminated pixels."""
    image_path = Path(record["image_path"])
    mask_path = Path(record.get("mask_path") or "")
    if not image_path.is_file():
        raise ValueError(f"Immagine assente per la scena {record['scene_id']}.")
    if not record.get("mask_path") or not mask_path.is_file():
        raise ValueError(f"UDM2 assente per la scena {record['scene_id']}: mosaico sospeso.")
    confidence_min = float(config.get("confidence_min", 70))
    if not 0 <= confidence_min <= 100:
        raise ValueError("La confidenza UDM2 deve essere compresa tra 0 e 100.")
    classes = config.get("mask_classes", list(MASK_BANDS))
    unknown = set(classes) - set(MASK_BANDS)
    if unknown:
        raise ValueError("Classi UDM2 sconosciute: " + ", ".join(sorted(unknown)))
    clean_path = tempdir / f"source_{sequence:06d}.tif"
    score_path = tempdir / f"score_{sequence:06d}.tif"
    with rasterio.open(image_path) as src, rasterio.open(mask_path) as mask:
        if not src.crs or not mask.crs:
            raise ValueError(f"CRS assente in immagine o UDM2: {record['scene_id']}.")
        if mask.count < 8:
            raise ValueError(f"La maschera di {record['scene_id']} non è UDM2 a 8 bande.")
        source_zone = _project(zone_wgs84, "EPSG:4326", src.crs)
        try:
            crop = geometry_window(src, [mapping(source_zone)], pad_x=2, pad_y=2)
        except WindowError:
            return None
        cropped_grid = dict(
            width=int(crop.width), height=int(crop.height), crs=src.crs,
            transform=src.window_transform(crop),
        )
        profile = _profile(cropped_grid, src.count, "float32", NODATA)
        score_profile = _profile(cropped_grid, 1, "uint16", SCORE_NODATA)
        # 255 is outside the documented range of all UDM2 classes/confidence.
        # Explicitly override nodata=0: zero class bits are legitimate UDM2 values.
        with WarpedVRT(
            mask, crs=src.crs, transform=src.transform, width=src.width, height=src.height,
            src_nodata=255, nodata=255, resampling=Resampling.nearest,
            warp_mem_limit=64,
        ) as aligned_mask, rasterio.open(clean_path, "w", **profile) as clean, rasterio.open(
            score_path, "w", **score_profile
        ) as scores:
            clean.scales = src.scales
            clean.offsets = src.offsets
            for window in _windows(cropped_grid["width"], cropped_grid["height"]):
                _check_cancel(cancel)
                read_window = Window(crop.col_off + window.col_off, crop.row_off + window.row_off,
                                     window.width, window.height)
                native = src.read(window=read_window, masked=True, out_dtype="float32")
                udm = aligned_mask.read(list(range(1, 9)), window=read_window)
                valid = ~np.ma.getmaskarray(native).any(axis=0)
                valid &= np.isfinite(native.data).all(axis=0)
                valid &= (udm[7] == 0) & (udm[6] >= confidence_min) & (udm[6] <= 100)
                valid &= (udm[:6] > 0).any(axis=0)
                if config.get("clear_only", True):
                    valid &= udm[0] == 1
                for class_name in classes:
                    valid &= udm[MASK_BANDS[class_name] - 1] == 0
                values = native.filled(NODATA)
                values[:, ~valid] = NODATA
                score = np.full(valid.shape, SCORE_NODATA, dtype=np.uint16)
                score[valid] = ((udm[0][valid] == 1).astype(np.uint16) * 101
                                + udm[6][valid].astype(np.uint16))
                clean.write(values, window=window)
                scores.write(score, 1, window=window)
        return dict(
            record=record, image_path=clean_path, score_path=score_path,
            count=src.count, scales=list(src.scales), offsets=list(src.offsets),
            descriptions=[text or f"band_{i + 1}" for i, text in enumerate(src.descriptions)],
            source_dtypes=list(src.dtypes),
        )


class _WarpCache:
    """Bound the number of open GDAL datasets even on days with many scenes."""
    def __init__(self, grid, maximum=8):
        self.grid = grid
        self.maximum = maximum
        self.entries = OrderedDict()

    def get(self, prepared):
        key = str(prepared["image_path"])
        if key in self.entries:
            stack, image, score = self.entries.pop(key)
            self.entries[key] = (stack, image, score)
            return image, score
        stack = ExitStack()
        try:
            image_src = stack.enter_context(rasterio.open(prepared["image_path"]))
            score_src = stack.enter_context(rasterio.open(prepared["score_path"]))
            common = dict(crs=self.grid["crs"], transform=self.grid["transform"],
                          width=self.grid["width"], height=self.grid["height"], warp_mem_limit=64)
            image = stack.enter_context(WarpedVRT(image_src, src_nodata=NODATA, nodata=NODATA,
                                                 resampling=Resampling.bilinear, **common))
            score = stack.enter_context(WarpedVRT(score_src, src_nodata=SCORE_NODATA,
                                                 nodata=SCORE_NODATA,
                                                 resampling=Resampling.nearest, **common))
        except Exception:
            stack.close()
            raise
        self.entries[key] = (stack, image, score)
        if len(self.entries) > self.maximum:
            _, (previous, _, _) = self.entries.popitem(last=False)
            previous.close()
        return image, score

    def close(self):
        for stack, _, _ in self.entries.values():
            stack.close()
        self.entries.clear()


def _overlap(first, second):
    return first[0] < second[2] and first[2] > second[0] and first[1] < second[3] and first[3] > second[1]


def _mosaic_date(date, prepared, grid, metadata, indices, destination, staging, config, emit, cancel):
    count = metadata["count"]
    paths = {"mosaic": staging / "mosaic.tif", "valid_mask": staging / "valid_mask.tif",
             "provenance": staging / "provenance.tif"}
    valid_pixels = 0
    aoi_pixels = 0
    source_pixels = defaultdict(int)
    cache = _WarpCache(grid)
    try:
        with rasterio.open(paths["mosaic"], "w", **_profile(grid, count, "float32", NODATA)) as mosaic, \
             rasterio.open(paths["valid_mask"], "w", **_profile(grid, 1, "uint8", 0)) as valid_file, \
             rasterio.open(paths["provenance"], "w", **_profile(grid, 1, "uint32", 0)) as provenance_file:
            mosaic.scales = tuple(metadata["scales"])
            mosaic.offsets = tuple(metadata["offsets"])
            for i, description in enumerate(metadata["descriptions"], 1):
                mosaic.set_band_description(i, description)
            mosaic.update_tags(ACQUISITION_DATE_UTC=date, NUMERIC_SCALE="native values retained; float32",
                               MASKING="UDM2 applied before bilinear interpolation; nearest validity gate",
                               SOURCE_SELECTION="clear class, confidence, scene clear/cloud percentage, scene ID")
            valid_file.set_band_description(1, "1=valid; 0=invalid/outside")
            provenance_file.set_band_description(1, "scene index; 0=invalid/outside; see metadata.json")
            for window in _windows(grid["width"], grid["height"]):
                _check_cancel(cancel)
                transform = window_transform(window, grid["transform"])
                dimensions = (int(window.height), int(window.width))
                inside = geometry_mask([mapping(grid["geometry"])], out_shape=dimensions,
                                       transform=transform, invert=True)
                aoi_pixels += int(inside.sum())
                output = np.full((count, *dimensions), NODATA, dtype=np.float32)
                best_score = np.full(dimensions, -1, dtype=np.int16)
                provenance = np.zeros(dimensions, dtype=np.uint32)
                if inside.any():
                    extent = window_bounds(window, grid["transform"])
                    for item in prepared:
                        _check_cancel(cancel)
                        if not _overlap(extent, item["zone_projected"].bounds):
                            continue
                        in_zone = geometry_mask([mapping(item["zone_projected"])], out_shape=dimensions,
                                                transform=transform, invert=True) & inside
                        if not in_zone.any():
                            continue
                        source, quality = cache.get(item)
                        score = quality.read(1, window=window)
                        eligible = in_zone & (score != SCORE_NODATA) & (score.astype(np.int32) > best_score)
                        if not eligible.any():
                            continue
                        values = source.read(window=window)
                        eligible &= (values != NODATA).all(axis=0) & np.isfinite(values).all(axis=0)
                        output[:, eligible] = values[:, eligible]
                        best_score[eligible] = score[eligible].astype(np.int16)
                        provenance[eligible] = indices[item["record"]["scene_id"]]
                valid = provenance != 0
                valid_pixels += int(valid.sum())
                keys, amounts = np.unique(provenance[valid], return_counts=True)
                for key, amount in zip(keys, amounts):
                    source_pixels[int(key)] += int(amount)
                mosaic.write(output, window=window)
                valid_file.write(valid.astype(np.uint8), 1, window=window)
                provenance_file.write(provenance, 1, window=window)
    finally:
        cache.close()
    result = {
        "date": date, "valid_pixels": valid_pixels, "aoi_pixels": aoi_pixels,
        "valid_percent_aoi_pixels": round(100 * valid_pixels / aoi_pixels, 4) if aoi_pixels else 0.0,
        "no_valid_pixels": valid_pixels == 0, "scene_count": len({p["record"]["scene_id"] for p in prepared}),
        "files": {key: str(destination / path.name) for key, path in paths.items()},
        "metadata_path": str(destination / "metadata.json"),
    }
    provenance_metadata = []
    for item in prepared:
        record = item["record"]
        index = indices[record["scene_id"]]
        provenance_metadata.append({
            "index": index, "scene_id": record["scene_id"], "zone_id": record["zone_id"],
            "acquired": record.get("acquired"), "coreg_status": record["coreg_status"],
            "image_path": str(record["image_path"]), "mask_path": str(record["mask_path"]),
            "quality_path": str(record["quality_path"]) if record.get("quality_path") else None,
            "source_scales": item["scales"], "source_offsets": item["offsets"],
        })
    document = dict(result, grid=_grid_metadata(grid), nodata=NODATA,
                    bands=metadata["descriptions"], scales=metadata["scales"], offsets=metadata["offsets"],
                    numeric_scale="Original numeric values; no reflectance/radiance scaling applied.",
                    ranking="Per-pixel clear class, then UDM2 confidence; ties: scene clear descending, cloud ascending, scene ID, zone ID.",
                    provenance_zero="No usable pixel / outside AOI or zone",
                    source_pixel_counts={str(key): value for key, value in sorted(source_pixels.items())},
                    sources=provenance_metadata,
                    masking={"clear_only": bool(config.get("clear_only", True)),
                             "mask_classes": config.get("mask_classes", list(MASK_BANDS)),
                             "confidence_min": config.get("confidence_min", 70),
                             "unusable_band8_always_excluded": True,
                             "before_resampling": True})
    _write_json(staging / "metadata.json", document)
    _check_cancel(cancel)
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("mosaic.tif", "valid_mask.tif", "provenance.tif", "metadata.json"):
        os.replace(staging / filename, destination / filename)
    if valid_pixels == 0:
        emit(f"{date}: nessun pixel utilizzabile dopo maschere e ritaglio; file NoData conservati.")
    else:
        emit(f"{date}: mosaico salvato, {result['valid_percent_aoi_pixels']:.1f}% dei pixel AOI utilizzabili.")
    return result


def process_area(area_plan, records, config, output_dir: Path, emit=lambda message: None, cancel=None):
    """Write common-grid daily mosaics, masks and provenance for one feature AOI.

    ``output_dir`` is this area's own output directory.  Only records proven to be
    a coregistered target or a fixed anchor are accepted.  Repeated anchors are
    deduplicated per (zone, scene).  Failed/missing masks stop processing visibly.
    """
    _check_cancel(cancel)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    area_geometry = _geometry(area_plan["geometry"])
    grid = _make_grid(area_geometry, config)
    zones = {}
    for zone in area_plan.get("zones", []):
        clipped = _polygonal(_geometry(zone["geometry"]).intersection(area_geometry))
        if not clipped.is_empty:
            zones[str(zone["id"])] = clipped
    unique = {}
    for record in sorted(records, key=_sort_key):
        _check_cancel(cancel)
        if record.get("coreg_status") not in {"anchor", "coregistered"}:
            raise ValueError(f"Scena non coregistrata: {record.get('scene_id', '?')}.")
        if str(record["zone_id"]) not in zones:
            raise ValueError(f"Zona assente o vuota: {record['zone_id']}.")
        key = (str(record["zone_id"]), str(record["scene_id"]))
        if key not in unique:
            unique[key] = record
    scene_ids = sorted({record["scene_id"] for record in unique.values()})
    if len(scene_ids) >= 2**32:
        raise ValueError("Troppi identificatori di scena per la provenance uint32.")
    indices = {scene: index + 1 for index, scene in enumerate(scene_ids)}
    groups = defaultdict(list)
    for record in unique.values():
        groups[_date(record)].append(record)
    summary = {
        "area_id": area_plan["id"], "area_name": area_plan.get("name", area_plan["id"]),
        "grid": _grid_metadata(grid), "dates": [], "scene_count": len(scene_ids),
        "input_records": len(records), "unique_zone_scene_records": len(unique),
        "duplicate_records_removed": len(records) - len(unique), "warnings": [],
        "output_dir": str(output_dir), "day_count": 0, "valid_day_count": 0,
        "status": "complete", "outputs": [],
    }
    if not groups:
        summary["warnings"].append("Nessuna scena coregistrata disponibile per questa area.")
        summary["status"] = "partial"
        _write_json(output_dir / "summary.json", summary)
        return summary
    metadata = None
    with tempfile.TemporaryDirectory(prefix=".raster_work_", dir=output_dir) as temp:
        temporary = Path(temp)
        for date, group in sorted(groups.items()):
            _check_cancel(cancel)
            emit(f"{area_plan.get('name', area_plan['id'])}, {date}: maschera nativa e mosaico di {len(group)} scene/zone.")
            staging = temporary / date
            staging.mkdir()
            prepared = []
            for sequence, record in enumerate(sorted(group, key=_sort_key)):
                item = _prepare_source(record, zones[str(record["zone_id"])], config,
                                       staging, sequence, cancel)
                if item is None:
                    summary["warnings"].append(f"{record['scene_id']}: raster privo di sovrapposizione con la zona.")
                    continue
                if metadata is None:
                    metadata = item
                elif (metadata["count"] != item["count"] or metadata["scales"] != item["scales"]
                      or metadata["offsets"] != item["offsets"]):
                    raise ValueError("Scene con bande o scale/offset differenti: non si possono mosaicare senza conversione esplicita.")
                item["zone_projected"] = _project(zones[str(record["zone_id"])], "EPSG:4326", grid["crs"])
                prepared.append(item)
            if not prepared:
                summary["warnings"].append(f"{date}: nessun raster sovrapposto; data saltata.")
                continue
            result = _mosaic_date(date, prepared, grid, metadata, indices, output_dir / date,
                                  staging, config, emit, cancel)
            summary["dates"].append(result)
            summary["outputs"].append(result["files"])
            # Clean native intermediates after each day, bounding temporary disk use.
            for item in prepared:
                item["image_path"].unlink(missing_ok=True)
                item["score_path"].unlink(missing_ok=True)
            if result["no_valid_pixels"]:
                summary["warnings"].append(f"{date}: tutti i pixel risultano NoData.")
            _write_json(output_dir / "summary.json", summary)
    summary["day_count"] = len(summary["dates"])
    summary["valid_day_count"] = sum(not day["no_valid_pixels"] for day in summary["dates"])
    summary["status"] = "partial" if summary["warnings"] else "complete"
    _write_json(output_dir / "summary.json", summary)
    return summary
