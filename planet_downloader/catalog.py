"""Search, permission checks and reproducible order planning; never submit orders."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re

from pyproj import CRS, Transformer
from shapely.geometry import Polygon, shape
from shapely.ops import transform, unary_union

from .geometry import area_km2, as_geojson, polygonal, polygons, split_for_clip, validate_polygon

PRODUCTS = {
    "ps_sr_4b": {"label": "PlanetScope · surface reflectance · 4 bands", "item_type": "PSScene", "bundle": "analytic_sr_udm2", "asset": "ortho_analytic_4b_sr", "mask_asset": "ortho_udm2", "bands": 4, "resolution": 3.0},
    "ps_sr_8b": {"label": "PlanetScope · surface reflectance · 8 bands", "item_type": "PSScene", "bundle": "analytic_8b_sr_udm2", "asset": "ortho_analytic_8b_sr", "mask_asset": "ortho_udm2", "bands": 8, "resolution": 3.0},
    "ps_radiance_4b": {"label": "PlanetScope · radiance · 4 bands", "item_type": "PSScene", "bundle": "analytic_udm2", "asset": "ortho_analytic_4b", "mask_asset": "ortho_udm2", "bands": 4, "resolution": 3.0},
    "ps_radiance_8b": {"label": "PlanetScope · radiance · 8 bands", "item_type": "PSScene", "bundle": "analytic_8b_udm2", "asset": "ortho_analytic_8b", "mask_asset": "ortho_udm2", "bands": 8, "resolution": 3.0},
}
PUBLIC_CONFIG_KEYS = {
    "product", "start_date", "end_date", "cloud_scene_max", "aoi_cloud_max", "aoi_clear_min",
    "coverage_min", "quality_standard", "ground_control", "sun_elevation_min", "view_angle_max",
    "output_dir", "target_epsg", "resolution", "mask_classes", "clear_only", "confidence_min",
    "batch_size", "name_field", "layer",
}
PROPERTY_KEYS = {
    "acquired", "published", "updated", "item_type", "satellite_id", "instrument", "strip_id",
    "cloud_cover", "cloud_percent", "clear_percent", "snow_ice_percent", "shadow_percent",
    "light_haze_percent", "heavy_haze_percent", "quality_category", "ground_control",
    "sun_elevation", "sun_azimuth", "view_angle", "gsd", "pixel_resolution", "epsg_code",
    "anomalous_pixels", "black_fill", "publishing_stage", "visible_percent",
}


def _check_cancel(client, cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Operation stopped by the user.")
    if hasattr(client, "check_cancel"):
        client.check_cancel()


def utc_date_bounds(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    """Inclusive calendar dates as [start, midnight after end) in UTC."""
    if not all(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
               for value in (start_date, end_date)):
        raise ValueError("Dates must use the YYYY-MM-DD format.")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if end < start:
        raise ValueError("The end date is before the start date.")
    return (datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
            datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_filter(geometry: dict, config: dict) -> dict:
    """Asset filters are separate because each AssetFilter's list is OR."""
    product = PRODUCTS[config["product"]]
    start, stop = utc_date_bounds(config["start_date"], config["end_date"])
    filters = [
        {"type": "GeometryFilter", "field_name": "geometry", "config": geometry},
        {"type": "DateRangeFilter", "field_name": "acquired", "config": {"gte": _iso(start), "lt": _iso(stop)}},
        {"type": "PermissionFilter", "config": ["assets:download"]},
        {"type": "AssetFilter", "config": [product["asset"]]},
        {"type": "AssetFilter", "config": [product["mask_asset"]]},
    ]
    if config.get("cloud_scene_max") is not None:
        filters.append({"type": "RangeFilter", "field_name": "cloud_cover", "config": {"gte": 0, "lte": float(config["cloud_scene_max"]) / 100}})
    if config.get("quality_standard", True):
        filters.append({"type": "StringInFilter", "field_name": "quality_category", "config": ["standard"]})
    if config.get("ground_control", True):
        filters.append({"type": "StringInFilter", "field_name": "ground_control", "config": ["true"]})
    if config.get("sun_elevation_min") is not None:
        filters.append({"type": "RangeFilter", "field_name": "sun_elevation", "config": {"gte": float(config["sun_elevation_min"])}})
    if config.get("view_angle_max") is not None:
        filters.append({"type": "RangeFilter", "field_name": "view_angle", "config": {"gte": 0, "lte": float(config["view_angle_max"])}})
    return {"type": "AndFilter", "config": filters}


def _local_properties_match(properties: dict, config: dict) -> bool:
    """Defensive checks; malformed/missing active filter metadata fails closed."""
    if config.get("quality_standard", True) and properties.get("quality_category") != "standard":
        return False
    if config.get("ground_control", True) and str(properties.get("ground_control")).lower() != "true":
        return False
    for field, key, is_minimum in (("sun_elevation", "sun_elevation_min", True),
                                   ("view_angle", "view_angle_max", False),
                                   ("cloud_cover", "cloud_scene_max", False)):
        threshold = config.get(key)
        if threshold is None:
            continue
        value = _number(properties.get(field))
        if value is None:
            return False
        if field == "cloud_cover":
            value *= 100
            if not 0 <= value <= 100:
                return False
        if (is_minimum and value < float(threshold)) or (not is_minimum and value > float(threshold)):
            return False
    return True


def _assets_allowed(assets: dict, feature: dict, product: dict) -> bool:
    item_permissions = feature.get("_permissions") or []
    for name in (product["asset"], product["mask_asset"]):
        asset = assets.get(name)
        if not isinstance(asset, dict):
            return False
        perms = asset.get("_permissions") or []
        if "download" not in perms and f"assets.{name}:download" not in item_permissions:
            return False
    return True


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _anchor_rank(scene):
    clear = scene.get("aoi_clear_percent")
    if clear is None:
        clear = scene.get("clear_percent")
    cloud = scene.get("aoi_cloud_percent")
    if cloud is None:
        cloud = scene.get("cloud_percent")
    return (-(clear if clear is not None else -1), cloud if cloud is not None else 101,
            scene["acquired"], scene["id"])


def _small_partition_regions(geom):
    """API-invalid components/holes, without rounding away any real coverage."""
    for part in polygons(geom):
        if area_km2(part) < (1.0 - 1e-6) / 1_000_000:
            yield "component", part
        for ring in part.interiors:
            hole = Polygon(ring)
            if area_km2(hole) < (1.0 - 1e-6) / 1_000_000:
                yield "hole", hole


def _metric_context(region, metres=3.0):
    center = region.representative_point()
    crs = CRS.from_proj4(f"+proj=aeqd +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m +no_defs")
    to_m = Transformer.from_crs(4326, crs, always_xy=True).transform
    to_wgs = Transformer.from_crs(crs, 4326, always_xy=True).transform
    # A rectangle avoids adding hundreds of vertices around millimetric slivers.
    return transform(to_wgs, transform(to_m, region).envelope.buffer(metres, join_style=2))


def _repair_partition(assignments, footprints, cancel=None):
    """Give tiny residuals useful context by transferring overlap between anchors.

    Transfers are restricted to the recipient anchor footprint; they conserve
    the covered set and remove that same region from every donor. No scene or
    uncovered sliver is discarded. Impossible input is reported explicitly.
    """
    covered_set = polygonal(unary_union([entry["geometry"] for entry in assignments]))
    seen = set()
    for attempt in range(max(64, 16 * len(assignments))):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Operation stopped by the user.")
        issue = next(((entry, kind, region) for entry in assignments
                      for kind, region in _small_partition_regions(entry["geometry"])), None)
        if issue is None:
            return [entry for entry in assignments if not entry["geometry"].is_empty]
        receiver, kind, region = issue
        signature = tuple(entry["geometry"].wkb for entry in assignments)
        if signature in seen:
            break
        seen.add(signature)
        if kind == "hole":
            if region.difference(footprints[receiver["anchor_id"]]).area == 0:
                grown = region
            else:
                # Expand the anchor currently supplying the tiny hole, turning
                # the artificial hole into a valid larger partition region.
                donors = [(region.intersection(entry["geometry"]).area, index, entry)
                          for index, entry in enumerate(assignments) if entry is not receiver]
                overlap, _, donor = max(donors, key=lambda item: (item[0], -item[1]), default=(0, 0, None))
                if overlap <= 0:
                    break
                receiver = donor
                grown = None
        else:
            grown = None
        if grown is None:
            # Three metres is one native PlanetScope pixel. Increase context
            # only when the anchor/covered-set intersection is unusually narrow.
            for distance in (3.0, 10.0, 30.0, 100.0):
                grown = polygonal(_metric_context(region, distance)
                                  .intersection(footprints[receiver["anchor_id"]])
                                  .intersection(covered_set))
                if area_km2(grown) >= 1.0 / 1_000_000:
                    break
            if area_km2(grown) < 1.0 / 1_000_000:
                break
        old = receiver["geometry"]
        replacement = polygonal(old.union(grown))
        if replacement.equals(old):
            break
        receiver["geometry"] = replacement
        for donor in assignments:
            if donor is not receiver:
                donor["geometry"] = polygonal(donor["geometry"].difference(grown))
    raise ValueError(
        "Partition residual smaller than 1 m²: it cannot be assigned "
        "to a reference with a valid Planet clip without changing coverage. "
        "Partition the study area differently or slightly expand the vector boundary.")


def plan_zones(area_geometry: dict, scenes: list[dict], cancel=None) -> list[dict]:
    """Greedy fixed anchors; disjoint zones cover all available AOI portions.

    A scene is used in EVERY zone it overlaps, not assigned just to its best
    anchor. Thus spatial splitting never discards acquisitions or coverage.
    """
    if not scenes:
        return []
    area = shape(area_geometry)
    footprints = {scene["id"]: polygonal(shape(scene["geometry"]).intersection(area)) for scene in scenes}
    remaining = polygonal(unary_union(list(footprints.values())))
    assignments = []
    ranked = sorted(scenes, key=_anchor_rank)
    while not remaining.is_empty:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Operation stopped by the user.")
        candidates = [(area_km2(footprints[scene["id"]].intersection(remaining)), index, scene)
                      for index, scene in enumerate(ranked)]
        coverage, _, anchor = max(candidates, key=lambda row: (row[0], -row[1]))
        if coverage <= 0:
            raise ValueError("Cannot partition the entire available area for coregistration.")
        covered = polygonal(footprints[anchor["id"]].intersection(remaining))
        assignments.append({"anchor_id": anchor["id"], "geometry": covered})
        # Subtract the original footprint, not its freshly intersected copy:
        # the latter can manufacture degenerate edge triangles through a
        # second floating-point overlay even when the footprint fully covers it.
        remaining = polygonal(remaining.difference(footprints[anchor["id"]]))
        ranked = [scene for scene in ranked if scene["id"] != anchor["id"]]
        if not ranked and not remaining.is_empty:
            raise ValueError("Geometric residuals in the partition: check the vector geometries.")
    assignments = _repair_partition(assignments, footprints, cancel)
    zones = []
    for assignment in assignments:
        anchor_id = assignment["anchor_id"]
        for part in split_for_clip(assignment["geometry"]):
            ids = [scene["id"] for scene in scenes
                   if not footprints[scene["id"]].intersection(part).is_empty
                   and footprints[scene["id"]].intersection(part).area > 0]
            geo = as_geojson(part)
            zone_id = f"zone_{len(zones)+1:03d}_{_hash([anchor_id, geo])[:8]}"
            zones.append({"id": zone_id, "geometry": geo, "anchor_id": anchor_id, "scene_ids": ids})
    return zones


def plan_orders(area_id: str, zones: list[dict], scenes: list[dict], config: dict) -> list[dict]:
    product = PRODUCTS[config["product"]]
    batch_size = int(config.get("batch_size", 100))
    if not 1 <= batch_size <= 499:
        raise ValueError("A batch must contain 1 to 499 images in addition to the fixed reference.")
    geometries = {scene["id"]: shape(scene["geometry"]) for scene in scenes}
    orders = []
    for zone in zones:
        targets = [item for item in dict.fromkeys(zone["scene_ids"]) if item != zone["anchor_id"]]
        batches = [targets[n:n + batch_size] for n in range(0, len(targets), batch_size)] or [[]]
        for batch in batches:
            item_ids = [zone["anchor_id"], *batch]
            identity = {"area": area_id, "zone": zone["geometry"], "anchor": zone["anchor_id"],
                        "items": item_ids, "product": config["product"]}
            key = _hash(identity)[:24]
            payload = {"name": f"PlanetLocal_{key}", "source_type": "scenes", "order_type": "full",
                       "products": [{"item_ids": item_ids, "item_type": product["item_type"], "product_bundle": product["bundle"]}],
                       "tools": [{"coregister": {"anchor_item": zone["anchor_id"]}},
                                 {"clip": {"aoi": zone["geometry"]}}], "metadata": {"stac": {}}}
            zone_geometry = shape(zone["geometry"])
            orders.append({"key": key, "zone_id": zone["id"], "anchor_id": zone["anchor_id"],
                           "item_ids": item_ids, "target_ids": batch, "payload": payload,
                           "estimated_km2": sum(area_km2(geometries[item].intersection(zone_geometry)) for item in item_ids),
                           "full_scene_km2": sum(area_km2(geometries[item]) for item in item_ids)})
    return orders


def _primed_candidates(client, candidates, item_type, band, emit, cancel):
    """Overlap UDM2 activation for at most eight candidates, then evaluate them.

    Priming one active band activates the shared UDM2 asset. The second quality
    filter remains conditional on the first, as before. Older/custom clients
    without nonblocking priming retain the original polling-only behaviour.
    """
    prime = getattr(client, "prime_coverage", None)
    for offset in range(0, len(candidates), 8):
        batch = candidates[offset:offset + 8]
        if band is not None and callable(prime):
            emit(f"Starting UDM2 for images {offset + 1}–{offset + len(batch)}/{len(candidates)}.")
            for item_id, _, _, _, intersection, _, _ in batch:
                _check_cancel(client, cancel)
                prime(item_type, item_id, as_geojson(intersection), band, cancel)
        yield from batch


def build_plan(client, areas: list[dict], config: dict, emit=lambda _: None, cancel=None) -> dict:
    config = {key: deepcopy(value) for key, value in config.items() if key in PUBLIC_CONFIG_KEYS}
    if config.get("product") not in PRODUCTS:
        raise ValueError("Unsupported product.")
    start, stop = utc_date_bounds(config["start_date"], config["end_date"])
    product = PRODUCTS[config["product"]]
    asset_cache = {}
    unique_footprints = {}
    clipped_by_scene = {}
    area_plans = []
    warnings = ["The km² estimate is geometric: actual consumption depends on the Planet contract and billing rules. Repeated references in separate batches are included.",
                "Coregistration is checked after download: images not aligned by Planet are excluded from mosaics and reported."]
    if config.get("aoi_cloud_max") is not None or config.get("aoi_clear_min") is not None:
        warnings.append("AOI filters use UDM2 only over the scene–area intersection; searching may activate UDM2 masks and take several minutes.")
    for area_index, original_area in enumerate(areas, start=1):
        _check_cancel(client, cancel)
        area = {key: deepcopy(original_area[key]) for key in ("id", "name", "geometry", "area_km2")}
        geometry = validate_polygon(shape(area["geometry"]), f"Area {area['name']}", minimum_rings=True)
        area["area_km2"] = area_km2(geometry)
        emit(f"Searching area {area_index}/{len(areas)}: {area['name']}.")
        scenes, area_warnings, seen = [], [], set()
        candidates = []
        discarded = Counter()
        for feature in client.search(product["item_type"], build_filter(area["geometry"], config)):
            _check_cancel(client, cancel)
            item_id = feature.get("id")
            if not isinstance(item_id, str) or not item_id or len(item_id) > 150 or not re.fullmatch(r"[a-zA-Z0-9_-]+", item_id):
                discarded["invalid metadata"] += 1
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            props = feature.get("properties", {})
            try:
                acquired = datetime.fromisoformat(str(props["acquired"]).replace("Z", "+00:00"))
                if acquired.tzinfo is None:
                    raise ValueError("Timestamp without a time zone")
                acquired = acquired.astimezone(timezone.utc)
                footprint = validate_polygon(shape(feature["geometry"]), "Scene footprint")
            except (ValueError, KeyError, TypeError):
                discarded["invalid geometry or date"] += 1
                continue
            if not start <= acquired < stop or not _local_properties_match(props, config):
                discarded["scene filters"] += 1
                continue
            intersection = polygonal(geometry.intersection(footprint))
            intersect_km2 = area_km2(intersection)
            cover = min(100.0, 100 * intersect_km2 / area["area_km2"])
            if intersect_km2 <= 0 or cover + 1e-8 < float(config.get("coverage_min", 0)):
                discarded["geometric coverage"] += 1
                continue
            if item_id not in asset_cache:
                asset_cache[item_id] = client.item_assets(product["item_type"], item_id)
            if not _assets_allowed(asset_cache[item_id], feature, product):
                discarded["missing or unauthorized assets"] += 1
                continue
            candidates.append((item_id, props, acquired, footprint, intersection, intersect_km2, cover))
        first_band = ("cloud" if config.get("aoi_cloud_max") is not None else
                      "clear" if config.get("aoi_clear_min") is not None else None)
        for item_id, props, acquired, footprint, intersection, intersect_km2, cover in _primed_candidates(
                client, candidates, product["item_type"], first_band, emit, cancel):
            _check_cancel(client, cancel)
            aoi_cloud, aoi_clear = None, None
            if config.get("aoi_cloud_max") is not None:
                emit(f"{area['name']}: UDM2 cloud coverage over the AOI, scene {item_id}.")
                aoi_cloud = _number(client.coverage(product["item_type"], item_id, as_geojson(intersection), "cloud", cancel))
                if aoi_cloud is None or not 0 <= aoi_cloud <= 100:
                    raise ValueError(f"Scene {item_id}: invalid UDM2 cloud percentage; search incomplete.")
                if aoi_cloud > float(config["aoi_cloud_max"]):
                    discarded["AOI cloud coverage"] += 1
                    continue
            if config.get("aoi_clear_min") is not None:
                emit(f"{area['name']}: UDM2 clear pixels over the AOI, scene {item_id}.")
                aoi_clear = _number(client.coverage(product["item_type"], item_id, as_geojson(intersection), "clear", cancel))
                if aoi_clear is None or not 0 <= aoi_clear <= 100:
                    raise ValueError(f"Scene {item_id}: invalid UDM2 clear percentage; search incomplete.")
                if aoi_clear < float(config["aoi_clear_min"]):
                    discarded["AOI clear pixels"] += 1
                    continue
            cloud_cover = _number(props.get("cloud_cover"))
            safe_props = {key: value for key, value in props.items()
                          if key in PROPERTY_KEYS and isinstance(value, (str, int, float, bool, type(None)))
                          and not (isinstance(value, float) and not math.isfinite(value))}
            scenes.append({"id": item_id, "acquired": _iso(acquired), "date": acquired.date().isoformat(),
                           "geometry": as_geojson(footprint),
                           "cloud_percent": cloud_cover * 100 if cloud_cover is not None else _number(props.get("cloud_percent")),
                           "clear_percent": _number(props.get("clear_percent")), "coverage_percent": cover,
                           "aoi_cloud_percent": aoi_cloud, "aoi_clear_percent": aoi_clear,
                           "clipped_km2": intersect_km2, "properties": safe_props})
            unique_footprints[item_id] = footprint
            clipped_by_scene.setdefault(item_id, []).append(intersection)
        scenes.sort(key=lambda scene: (scene["acquired"], scene["id"]))
        if discarded:
            area_warnings.append("Excluded scenes: " + "; ".join(f"{reason}: {count}" for reason, count in sorted(discarded.items())) + ".")
        if not scenes:
            area_warnings.append("No authorized images satisfy all filters for this area.")
        emit(f"{area['name']}: {len(scenes)} eligible images; preparing fixed references and the estimate.")
        zones = plan_zones(area["geometry"], scenes, cancel)
        if len({zone["anchor_id"] for zone in zones}) > 1:
            area_warnings.append("The area requires multiple references: it is divided into non-overlapping zones, each with a fixed reference. Planet does not guarantee alignment between zones.")
        orders = plan_orders(area["id"], zones, scenes, config)
        area.update({"scenes": scenes, "zones": zones, "orders": orders, "warnings": area_warnings,
                     "available_coverage_percent": min(100.0, 100 * area_km2(unary_union([shape(z["geometry"]) for z in zones])) / area["area_km2"]) if zones else 0.0})
        area_plans.append(area)
    all_orders = [order for area in area_plans for order in area["orders"]]
    totals = {
        "scene_count": sum(len(area["scenes"]) for area in area_plans),
        "unique_scene_count": len(unique_footprints), "area_count": len(area_plans),
        "day_count": sum(len({scene["date"] for scene in area["scenes"]}) for area in area_plans),
        "order_count": len(all_orders), "estimated_km2": sum(order["estimated_km2"] for order in all_orders),
        "unique_clipped_km2": sum(area_km2(unary_union(parts)) for parts in clipped_by_scene.values()),
        "full_scene_km2": sum(area_km2(geom) for geom in unique_footprints.values()),
        "ordered_full_scene_km2": sum(order["full_scene_km2"] for order in all_orders),
        "aoi_km2": sum(area["area_km2"] for area in area_plans),
        "ordered_scene_count": sum(len(order["item_ids"]) for order in all_orders),
        "anchor_delivery_count": len(all_orders),
    }
    _check_cancel(client, cancel)
    return {"version": 1, "created_at": _iso(datetime.now(timezone.utc)), "config": config,
            "areas": area_plans, "totals": totals, "warnings": warnings}
