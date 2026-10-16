from datetime import datetime, timezone
import json
import threading

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, Polygon, box, shape
from shapely.ops import transform, unary_union

from planet_downloader.catalog import PRODUCTS, build_filter, build_plan, plan_orders, plan_zones, utc_date_bounds
from planet_downloader.geometry import area_km2, as_geojson, load_areas, split_for_clip, validate_polygon, vertex_count


def settings(**overrides):
    return {"product": "ps_sr_4b", "start_date": "2026-01-01", "end_date": "2026-01-31",
            "cloud_scene_max": 30, "aoi_cloud_max": None, "aoi_clear_min": None,
            "coverage_min": 0, "quality_standard": True, "ground_control": True,
            "sun_elevation_min": 0, "view_angle_max": 30, "batch_size": 100, **overrides}


def feature(item_id="A", geom=None, acquired="2026-01-15T11:00:00Z", **props):
    return {"id": item_id, "geometry": as_geojson(geom if geom is not None else box(11, 44, 11.02, 44.02)),
            "properties": {"acquired": acquired, "cloud_cover": .1, "clear_percent": 80,
                           "quality_category": "standard", "ground_control": True,
                           "sun_elevation": 45, "view_angle": 5, **props}}


def area(geom=None, name="Area"):
    geom = geom if geom is not None else box(11.005, 44.005, 11.015, 44.015)
    return {"id": name, "name": name, "geometry": as_geojson(geom), "area_km2": area_km2(geom)}


class Client:
    def __init__(self, features=(), assets=None):
        self.features = features
        self.assets = assets
        self.searches = []
        self.coverages = []
        self.asset_calls = []

    def search(self, item_type, filter_dict):
        self.searches.append((item_type, filter_dict))
        yield from self.features

    def item_assets(self, item_type, item_id):
        self.asset_calls.append(item_id)
        if self.assets is not None:
            return self.assets
        return {"ortho_analytic_4b_sr": {"_permissions": ["download"]},
                "ortho_udm2": {"_permissions": ["download"]}}

    def coverage(self, item_type, item_id, geometry, band, cancel):
        self.coverages.append((item_id, geometry, band))
        return 10 if band == "cloud" else 90


def test_date_bounds_include_whole_final_date_and_leap_day():
    start, stop = utc_date_bounds("2024-02-28", "2024-02-29")
    assert start == datetime(2024, 2, 28, tzinfo=timezone.utc)
    assert stop == datetime(2024, 3, 1, tzinfo=timezone.utc)
    f = build_filter(area()["geometry"], settings())
    dates = next(x for x in f["config"] if x["type"] == "DateRangeFilter")["config"]
    assert dates == {"gte": "2026-01-01T00:00:00Z", "lt": "2026-02-01T00:00:00Z"}
    with pytest.raises(ValueError):
        utc_date_bounds("2026-02-30", "2026-03-02")
    with pytest.raises(ValueError):
        utc_date_bounds("2026-01-02", "2026-01-01")


def test_filters_require_both_assets_not_or():
    f = build_filter(area()["geometry"], settings())
    assets = [x["config"] for x in f["config"] if x["type"] == "AssetFilter"]
    assert assets == [["ortho_analytic_4b_sr"], ["ortho_udm2"]]
    assert {"type": "PermissionFilter", "config": ["assets:download"]} in f["config"]
    assert next(x for x in f["config"] if x.get("field_name") == "cloud_cover")["config"]["lte"] == .3


def test_geometry_preserves_holes_multipart_and_reprojects(tmp_path):
    with_hole = Polygon([(11, 44), (11.02, 44), (11.02, 44.02), (11, 44.02), (11, 44)],
                        holes=[[(11.005, 44.005), (11.015, 44.005), (11.015, 44.015), (11.005, 44.015), (11.005, 44.005)]])
    geom = MultiPolygon([with_hole, box(11.03, 44, 11.04, 44.01)])
    frame = gpd.GeoDataFrame({"name": ["Bosco / à"]}, geometry=[geom], crs=4326).to_crs(32632)
    file = tmp_path / "areas.gpkg"
    frame.to_file(file, layer="boschi", engine="pyogrio")
    result = load_areas(str(file), name_field="name")
    loaded = shape(result[0]["geometry"])
    assert loaded.geom_type == "MultiPolygon"
    assert sum(len(part.interiors) for part in loaded.geoms) == 1
    assert loaded.symmetric_difference(geom).area < 1e-12
    assert result == load_areas(str(file), name_field="name")
    assert "/" not in result[0]["id"]
    assert result[0]["area_km2"] == pytest.approx(area_km2(geom), rel=1e-8)


def test_missing_crs_is_not_guessed(tmp_path):
    file = tmp_path / "unknown.shp"
    frame = gpd.GeoDataFrame(geometry=[box(500000, 4900000, 500100, 4900100)])
    with pytest.warns(UserWarning):
        frame.to_file(file, engine="pyogrio")
    with pytest.raises(ValueError, match="CRS assente"):
        load_areas(str(file))


def test_multilayer_requires_selection_and_missing_field_fails(tmp_path):
    file = tmp_path / "areas.gpkg"
    frame = gpd.GeoDataFrame({"name": ["A"]}, geometry=[box(11, 44, 11.01, 44.01)], crs=4326)
    frame.to_file(file, layer="one", engine="pyogrio")
    frame.to_file(file, layer="two", engine="pyogrio", append=False)
    with pytest.raises(ValueError, match="più layer"):
        load_areas(str(file))
    assert len(load_areas(str(file), layer="two")) == 1
    with pytest.raises(ValueError, match="Campo nome"):
        load_areas(str(file), layer="two", name_field="missing")


def test_exact_split_retains_all_vertices_and_area():
    geom = Point(11, 44).buffer(.03, quad_segs=600)
    pieces = split_for_clip(geom)
    assert len(pieces) > 1
    assert max(vertex_count(part) for part in pieces) <= 1500
    merged = unary_union(pieces)
    assert merged.symmetric_difference(geom).area < 1e-15
    for i, first in enumerate(pieces):
        for second in pieces[i + 1:]:
            assert first.intersection(second).area == 0


def test_zero_results_has_no_orders_or_coverage_and_zero_totals():
    client = Client()
    plan = build_plan(client, [area()], settings())
    assert not plan["areas"][0]["orders"]
    assert plan["totals"]["estimated_km2"] == 0
    assert plan["totals"]["scene_count"] == 0
    assert not client.coverages


@pytest.mark.parametrize("assets", [
    {"ortho_analytic_4b_sr": {"_permissions": ["download"]}},
    {"ortho_analytic_4b_sr": {"_permissions": []}, "ortho_udm2": {"_permissions": ["download"]}},
])
def test_missing_or_unpermitted_assets_excluded_before_coverage(assets):
    client = Client([feature()], assets=assets)
    plan = build_plan(client, [area()], settings(aoi_clear_min=70))
    assert plan["totals"]["scene_count"] == 0
    assert not client.coverages
    assert "asset mancanti" in plan["areas"][0]["warnings"][0]


def test_coverage_uses_intersection_and_is_separate_from_scene_coverage():
    aoi = box(11, 44, 11.02, 44.02)
    scene_geometry = box(11.01, 44, 11.03, 44.02)
    client = Client([feature(geom=scene_geometry)])
    plan = build_plan(client, [area(aoi)], settings(aoi_clear_min=70, aoi_cloud_max=20))
    scene = plan["areas"][0]["scenes"][0]
    assert scene["coverage_percent"] == pytest.approx(50, rel=1e-3)
    assert scene["aoi_clear_percent"] == 90
    assert scene["aoi_cloud_percent"] == 10
    assert {call[2] for call in client.coverages} == {"clear", "cloud"}
    for _, geometry, _ in client.coverages:
        assert shape(geometry).equals(aoi.intersection(scene_geometry))


def test_last_day_end_exclusive_offset_timestamps_and_duplicate_page_items():
    client = Client([feature("A", acquired="2026-01-31T23:59:59.999999Z"),
                     feature("A", acquired="2026-01-31T23:59:59.999999Z"),
                     feature("B", acquired="2026-02-01T01:00:00+02:00"),
                     feature("C", acquired="2026-02-01T00:00:00Z")])
    plan = build_plan(client, [area()], settings())
    assert {item["id"] for item in plan["areas"][0]["scenes"]} == {"A", "B"}
    assert {item["date"] for item in plan["areas"][0]["scenes"]} == {"2026-01-31"}
    assert plan["totals"]["day_count"] == 1
    assert not client.coverages


def test_batch_anchor_repeated_and_billed_estimate_includes_anchor():
    client = Client([feature(f"S{i}", acquired=f"2026-01-{i+1:02d}T10:00:00Z") for i in range(4)])
    plan = build_plan(client, [area()], settings(batch_size=1))
    ap = plan["areas"][0]
    orders = ap["orders"]
    assert len(orders) == 3
    assert len({order["anchor_id"] for order in orders}) == 1
    assert all(len(order["item_ids"]) == len(set(order["item_ids"])) == 2 for order in orders)
    assert {target for order in orders for target in order["target_ids"]} == {"S1", "S2", "S3"}
    assert plan["totals"]["estimated_km2"] == pytest.approx(area()["area_km2"] * 6, rel=1e-8)
    assert plan["totals"]["unique_clipped_km2"] == pytest.approx(area()["area_km2"] * 4, rel=1e-8)
    for order in orders:
        assert order["payload"]["tools"] == [
            {"coregister": {"anchor_item": order["anchor_id"]}},
            {"clip": {"aoi": ap["zones"][0]["geometry"]}}]
        assert not any(key in order["payload"] for key in ("delivery", "notifications"))


def test_large_aoi_partition_preserves_all_scene_intersections():
    aoi = box(11, 44, 11.04, 44.02)
    fs = [feature("left", box(11, 44, 11.025, 44.02)),
          feature("right", box(11.02, 44, 11.04, 44.02)),
          feature("middle", box(11.01, 44, 11.03, 44.02))]
    plan = build_plan(Client(fs), [area(aoi)], settings())
    ap = plan["areas"][0]
    geometries = {f["id"]: shape(f["geometry"]) for f in fs}
    zones = ap["zones"]
    assert len(zones) == 2
    assert unary_union([shape(z["geometry"]) for z in zones]).equals(aoi)
    assert shape(zones[0]["geometry"]).intersection(shape(zones[1]["geometry"])).area == 0
    for zone in zones:
        z = shape(zone["geometry"])
        assert z.difference(geometries[zone["anchor_id"]]).area == 0
        assert set(zone["scene_ids"]) == {key for key, geom in geometries.items() if geom.intersection(z).area > 0}
    assert any("più riferimenti" in warning for warning in ap["warnings"])


def test_overlapping_aoi_unique_area_and_asset_requests_deduplicated():
    a = area(box(11, 44, 11.015, 44.015), "A")
    b = area(box(11.005, 44.005, 11.02, 44.02), "B")
    client = Client([feature()])
    plan = build_plan(client, [a, b], settings(api_key="NEVER_PERSIST_ME"))
    assert plan["totals"]["scene_count"] == 2
    assert plan["totals"]["unique_scene_count"] == 1
    assert client.asset_calls == ["A"]
    expected = area_km2(shape(a["geometry"]).union(shape(b["geometry"])))
    assert plan["totals"]["unique_clipped_km2"] == pytest.approx(expected)
    assert "NEVER_PERSIST_ME" not in json.dumps(plan)


def test_single_scene_anchor_is_ordered_once_and_cancellation_stops_search():
    plan = build_plan(Client([feature()]), [area()], settings())
    order = plan["areas"][0]["orders"][0]
    assert order["item_ids"] == ["A"]
    assert order["target_ids"] == []
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        build_plan(Client([feature()]), [area()], settings(), cancel=event)


def test_order_bound_is_499_targets_plus_anchor():
    common = as_geojson(box(11, 44, 11.01, 44.01))
    scenes = [{"id": f"S{i}", "geometry": common} for i in range(501)]
    zones = [{"id": "z", "anchor_id": "S0", "geometry": common,
              "scene_ids": [scene["id"] for scene in scenes]}]
    orders = plan_orders("a", zones, scenes, settings(batch_size=499))
    assert [len(order["item_ids"]) for order in orders] == [500, 2]
    with pytest.raises(ValueError):
        plan_orders("a", zones, scenes, settings(batch_size=500))


def test_null_permissions_fail_closed_without_crashing():
    f = feature()
    f["_permissions"] = None
    assets = {"ortho_analytic_4b_sr": {"_permissions": None},
              "ortho_udm2": {"_permissions": ["download"]}}
    plan = build_plan(Client([f], assets=assets), [area()], settings())
    assert plan["totals"]["scene_count"] == 0


def test_millimetre_residual_redistributes_overlap_without_losing_coverage():
    to_wgs = Transformer.from_crs(32632, 4326, always_xy=True).transform
    aoi = transform(to_wgs, box(650000, 4900000, 650100, 4900100))
    first = transform(to_wgs, box(649999, 4899999, 650099.999, 4900101))
    second = transform(to_wgs, box(650000.001, 4899999, 650101, 4900101))
    plan = build_plan(Client([feature("A", first), feature("B", second)]), [area(aoi)], settings())
    zones = plan["areas"][0]["zones"]
    zone_shapes = [shape(zone["geometry"]) for zone in zones]
    assert len(zones) == 2
    assert all(area_km2(z) * 1e6 >= 1 for z in zone_shapes)
    assert unary_union(zone_shapes).symmetric_difference(aoi).area < 1e-15
    assert zone_shapes[0].intersection(zone_shapes[1]).area < 1e-20
    original = {"A": first, "B": second}
    for zone in zones:
        z = shape(zone["geometry"])
        validate_polygon(z, minimum_rings=True)
        assert z.difference(original[zone["anchor_id"]]).area < 1e-20
        assert set(zone["scene_ids"]) == {key for key, geom in original.items() if z.intersection(geom).area > 0}


def test_irreducible_small_covered_island_reports_partition_not_bad_input():
    to_wgs = Transformer.from_crs(32632, 4326, always_xy=True).transform
    aoi = transform(to_wgs, box(650000, 4900000, 650100, 4900100))
    first = transform(to_wgs, box(650000, 4900000, 650099, 4900100))
    tiny = transform(to_wgs, box(650099.8, 4900000, 650100, 4900000.2))
    with pytest.raises(ValueError, match="Residuo della partizione"):
        build_plan(Client([feature("A", first), feature("B", tiny)]), [area(aoi)], settings())


def test_udm2_primes_eight_candidates_before_polling_and_preserves_filter_results():
    class QualityClient(Client):
        def __init__(self, features):
            super().__init__(features)
            self.events = []

        def item_assets(self, item_type, item_id):
            self.events.append(("assets", item_id))
            if item_id == "missing":
                return {}
            return super().item_assets(item_type, item_id)

        def coverage(self, item_type, item_id, geometry, band, cancel):
            self.events.append(("poll", item_id, band))
            super().coverage(item_type, item_id, geometry, band, cancel)
            if item_id == "S0" and band == "cloud":
                return 90
            if item_id == "S1" and band == "clear":
                return 20
            return 10 if band == "cloud" else 90

    class PrimingClient(QualityClient):
        def prime_coverage(self, item_type, item_id, geometry, band="clear", cancel=None):
            self.events.append(("prime", item_id, band))
            assert shape(geometry).equals(shape(area()["geometry"]))

    features = ([feature(f"S{i}") for i in range(10)] +
                [feature("metadata_excluded", sun_elevation=-10), feature("missing")])
    config = settings(aoi_cloud_max=20, aoi_clear_min=70)
    serial, primed = QualityClient(features), PrimingClient(features)
    serial_plan = build_plan(serial, [area()], config)
    primed_plan = build_plan(primed, [area()], config)
    serial_plan.pop("created_at")
    primed_plan.pop("created_at")
    assert primed_plan == serial_plan
    assert primed_plan["totals"]["scene_count"] == 8
    events = [event for event in primed.events if event[0] != "assets"]
    assert events[:8] == [("prime", f"S{i}", "cloud") for i in range(8)]
    next_window = events.index(("prime", "S8", "cloud"))
    expected_polls = [("poll", "S0", "cloud")]
    expected_polls += [("poll", f"S{i}", band) for i in range(1, 8) for band in ("cloud", "clear")]
    assert events[8:next_window] == expected_polls
    assert events[next_window:next_window + 2] == [("prime", "S8", "cloud"), ("prime", "S9", "cloud")]
    assert [event for event in primed.events if event[0] == "poll"] == [event for event in serial.events if event[0] == "poll"]
    assert primed.events.index(("assets", "missing")) < primed.events.index(("prime", "S0", "cloud"))
    assert not any(event[1] in {"metadata_excluded", "missing"} for event in events)


@pytest.mark.parametrize("clear_min,expected_band", [(70, "clear"), (None, None)])
def test_udm2_priming_uses_only_first_active_filter(clear_min, expected_band):
    class PrimingClient(Client):
        def __init__(self):
            super().__init__([feature()])
            self.primed = []

        def prime_coverage(self, item_type, item_id, geometry, band="clear", cancel=None):
            self.primed.append((item_id, band))

    client = PrimingClient()
    build_plan(client, [area()], settings(aoi_clear_min=clear_min))
    assert client.primed == ([("A", expected_band)] if expected_band is not None else [])


def test_cancellation_during_priming_stops_before_remaining_activation_or_poll():
    event = threading.Event()

    class PrimingClient(Client):
        def __init__(self):
            super().__init__([feature("A"), feature("B")])
            self.primed = []

        def prime_coverage(self, item_type, item_id, geometry, band="clear", cancel=None):
            self.primed.append(item_id)
            event.set()

    client = PrimingClient()
    with pytest.raises(InterruptedError):
        build_plan(client, [area()], settings(aoi_cloud_max=20), cancel=event)
    assert client.primed == ["A"]
    assert client.coverages == []
