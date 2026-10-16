"""Validated application parameters; credentials are deliberately separate."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import math


DEFAULTS = {
    "product": "ps_sr_4b", "start_date": f"{date.today().year}-01-01",
    "end_date": date.today().isoformat(), "cloud_scene_max": 30.0,
    "aoi_cloud_max": 20.0, "aoi_clear_min": 70.0, "coverage_min": 0.0,
    "quality_standard": True, "ground_control": True,
    "sun_elevation_min": 0.0, "view_angle_max": 30.0,
    "output_dir": str(Path.home() / "Planet_downloads"),
    "target_epsg": "", "resolution": 3.0,
    "mask_classes": ["snow", "shadow", "light_haze", "heavy_haze", "cloud"],
    "clear_only": True, "confidence_min": 70.0, "batch_size": 100,
    "name_field": "", "layer": "",
}


def validate_config(raw: dict) -> dict:
    """Accept only known non-secret settings and normalize GUI values."""
    from .catalog import PRODUCTS
    if not isinstance(raw, dict):
        raise ValueError("Settings must be a JSON object.")
    cfg = {k: raw.get(k, v) for k, v in DEFAULTS.items()}
    if cfg["product"] not in PRODUCTS:
        raise ValueError("Select a supported product.")
    try:
        start = date.fromisoformat(str(cfg["start_date"]))
        end = date.fromisoformat(str(cfg["end_date"]))
    except ValueError as exc:
        raise ValueError("Invalid dates: use YYYY-MM-DD.") from exc
    if start > end:
        raise ValueError("The start date must not be after the end date.")
    cfg.update(start_date=start.isoformat(), end_date=end.isoformat())
    for key, low, high, nullable in [
        ("cloud_scene_max", 0, 100, False), ("aoi_cloud_max", 0, 100, True),
        ("aoi_clear_min", 0, 100, True), ("coverage_min", 0, 100, False),
        ("sun_elevation_min", 0, 90, False), ("view_angle_max", 0, 90, False),
        ("confidence_min", 0, 100, False), ("resolution", 0.1, 1000, False),
    ]:
        value = cfg[key]
        if nullable and value in (None, ""):
            cfg[key] = None
            continue
        if isinstance(value, bool):
            raise ValueError(f"Invalid numeric value: {key}.")
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric value: {key}.") from exc
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key}: enter a value between {low} and {high}.")
        cfg[key] = value
    for key in ("quality_standard", "ground_control", "clear_only"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"Invalid option: {key}.")
    batch = cfg["batch_size"]
    try:
        if isinstance(batch, bool) or int(batch) != float(batch) or not 1 <= int(batch) <= 499:
            raise ValueError()
        cfg["batch_size"] = int(batch)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Batch size: choose an integer from 1 to 499.") from exc
    masks = cfg["mask_classes"]
    if not isinstance(masks, list) or any(x not in DEFAULTS["mask_classes"] for x in masks):
        raise ValueError("Invalid mask classes.")
    cfg["mask_classes"] = list(dict.fromkeys(masks))
    for key in ("name_field", "layer"):
        if not isinstance(cfg[key], str) or len(cfg[key]) > 200:
            raise ValueError(f"Invalid value: {key}.")
    raw_output = cfg["output_dir"]
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise ValueError("Choose an output folder.")
    output = Path(raw_output).expanduser()
    if not output.is_absolute():
        raise ValueError("The output folder must be an absolute path.")
    if output.exists() and not output.is_dir():
        raise ValueError("The selected destination is not a folder.")
    cfg["output_dir"] = str(output.resolve())
    epsg = str(cfg["target_epsg"] or "").strip().upper().removeprefix("EPSG:")
    if epsg:
        from pyproj import CRS
        try:
            crs = CRS.from_epsg(int(epsg))
            if not crs.is_projected or any(abs(a.unit_conversion_factor - 1) > 1e-9 for a in crs.axis_info[:2]):
                raise ValueError()
        except Exception as exc:
            raise ValueError("The output CRS must be a projected EPSG in metres (e.g. 32632).") from exc
        cfg["target_epsg"] = str(int(epsg))
    else:
        cfg["target_epsg"] = ""
    return cfg
