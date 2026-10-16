"""Vector input and exact polygon handling (WGS84 at the API boundary)."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

import geopandas as gpd
import pyogrio
from pyproj import CRS, Geod
from shapely import force_2d
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.validation import explain_validity

GEOD = Geod(ellps="WGS84")
MAX_CLIP_VERTICES = 1500


def polygonal(geom):
    """Keep only positive-area polygon components of a geometric overlay."""
    if geom.is_empty:
        return Polygon()
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        parts = [polygonal(part) for part in geom.geoms]
        return unary_union([part for part in parts if not part.is_empty])
    return Polygon()


def polygons(geom):
    geom = polygonal(geom)
    if isinstance(geom, Polygon):
        return [] if geom.is_empty else [geom]
    return list(geom.geoms)


def area_km2(geom) -> float:
    """Ellipsoidal area; orient each polygon so holes subtract, parts add."""
    if isinstance(geom, dict):
        geom = shape(geom)
    return sum(abs(GEOD.geometry_area_perimeter(orient(part, sign=1.0))[0])
               for part in polygons(geom)) / 1_000_000.0


def vertex_count(geom) -> int:
    return sum(len(part.exterior.coords) + sum(len(ring.coords) for ring in part.interiors)
               for part in polygons(geom))


def as_geojson(geom) -> dict:
    """Plain JSON lists (rather than tuples) and no non-geometry metadata."""
    return json.loads(json.dumps(mapping(geom), allow_nan=False))


def validate_polygon(geom, label: str = "Geometry", minimum_rings: bool = False):
    geom = force_2d(geom)
    if not isinstance(geom, (Polygon, MultiPolygon)) or geom.is_empty:
        raise ValueError(f"{label}: only non-empty Polygon and MultiPolygon geometries are supported.")
    if not geom.is_valid:
        raise ValueError(f"{label}: invalid geometry ({explain_validity(geom)}). Repair the vector file in QGIS.")
    bounds = geom.bounds
    if not all(math.isfinite(x) for x in bounds):
        raise ValueError(f"{label}: coordinates are not finite.")
    if bounds[0] < -180 or bounds[2] > 180 or bounds[1] < -90 or bounds[3] > 90:
        raise ValueError(f"{label}: coordinates are outside WGS84 bounds; check the vector CRS.")
    if bounds[2] - bounds[0] > 180:
        raise ValueError(f"{label}: geometries crossing the antimeridian are not supported; split them before loading.")
    if area_km2(geom) <= 0:
        raise ValueError(f"{label}: geographic area is zero.")
    if minimum_rings:
        for part in polygons(geom):
            if area_km2(part) * 1_000_000 < 1.0 - 1e-6:
                raise ValueError(f"{label}: a component is smaller than the 1 m² Planet clipping limit.")
            for ring in part.interiors:
                if area_km2(Polygon(ring)) * 1_000_000 < 1.0 - 1e-6:
                    raise ValueError(f"{label}: a hole is smaller than the 1 m² Planet clipping limit.")
    return geom


def list_layers(path: str) -> list[str]:
    return [str(row[0]) for row in pyogrio.list_layers(path)]


def load_areas(path: str, layer: str = "", name_field: str = "") -> list[dict]:
    """Each feature remains independent; no union, simplification, or hole removal."""
    source = Path(path)
    if source.suffix.lower() not in {".shp", ".gpkg", ".geojson", ".json"}:
        raise ValueError("Unsupported format. Use Shapefile, GeoPackage, or GeoJSON.")
    available = list_layers(str(source))
    if layer and layer not in available:
        raise ValueError(f"Layer '{layer}' not found. Available layers: {', '.join(available)}")
    if not layer and len(available) > 1:
        raise ValueError(f"The file contains multiple layers; select one of: {', '.join(available)}")
    frame = gpd.read_file(source, layer=layer or (available[0] if available else None), engine="pyogrio")
    if frame.crs is None:
        raise ValueError("Missing CRS: define the vector coordinate reference system (include the .prj file for Shapefiles).")
    CRS.from_user_input(frame.crs)  # Fail before trying any coordinate conversion.
    if name_field and name_field not in frame.columns:
        raise ValueError(f"Name field '{name_field}' not found. Fields: {', '.join(str(c) for c in frame.columns if c != frame.geometry.name)}")
    if frame.empty:
        raise ValueError("The layer contains no areas.")
    frame = frame.to_crs(epsg=4326)
    result = []
    for number, (_, row) in enumerate(frame.iterrows(), start=1):
        geom = row.geometry
        if geom is None:
            raise ValueError(f"Feature {number}: missing geometry.")
        geom = validate_polygon(geom, f"Feature {number}", minimum_rings=True)
        raw_name = row[name_field] if name_field else f"Area {number:03d}"
        name = str(raw_name).strip() if raw_name is not None else ""
        if not name or name.lower() in {"nan", "none", "<na>"}:
            name = f"Area {number:03d}"
        normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", normalized).strip("_-")[:48] or "area"
        geo = as_geojson(geom)
        digest = hashlib.sha256(json.dumps(geo, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:8]
        result.append({"id": f"area_{number:03d}_{slug}_{digest}", "name": name,
                       "geometry": geo, "area_km2": area_km2(geom)})
    return result


def split_for_clip(geom, max_vertices: int = MAX_CLIP_VERTICES) -> list:
    """Partition exactly with rectangles; never simplify or discard tiny features.

    Every output is API-valid. Pathological slivers are reported rather than
    silently changing the user's area. Shared edges have zero area.
    """
    if max_vertices < 8:
        raise ValueError("The vertex limit is too small.")
    geom = polygonal(geom)
    if geom.is_empty:
        return []
    pending = [(geom, 0)]
    result = []
    while pending:
        candidate, depth = pending.pop()
        if vertex_count(candidate) <= max_vertices:
            validate_polygon(candidate, "Clipping zone", minimum_rings=True)
            result.append(candidate)
            continue
        if depth >= 30 or len(result) + len(pending) > 10000:
            raise ValueError("Geometry is too complex for Planet clipping; split the vector into simpler areas.")
        # Components already disjoint can be separated without adding vertices.
        if isinstance(candidate, MultiPolygon):
            pending.extend((part, depth + 1) for part in reversed(list(candidate.geoms)))
            continue
        xmin, ymin, xmax, ymax = candidate.bounds
        if xmax - xmin >= ymax - ymin:
            middle = (xmin + xmax) / 2
            boxes = [box(xmin, ymin, middle, ymax), box(middle, ymin, xmax, ymax)]
        else:
            middle = (ymin + ymax) / 2
            boxes = [box(xmin, ymin, xmax, middle), box(xmin, middle, xmax, ymax)]
        pieces = [polygonal(candidate.intersection(bounds)) for bounds in boxes]
        pieces = [piece for piece in pieces if not piece.is_empty]
        if len(pieces) < 2:
            raise ValueError("Cannot split the geometry without changing it; repair the vector file.")
        pending.extend((piece, depth + 1) for piece in reversed(pieces))
    return sorted(result, key=lambda g: (g.bounds, g.wkb_hex))
