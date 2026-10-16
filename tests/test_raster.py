import json
from pathlib import Path
import threading

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box, mapping
from shapely.ops import transform

from planet_downloader.raster import NODATA, process_area


CRS = "EPSG:32632"
TRANSFORM = from_origin(500001, 1002, 3, 3)


def geographic(geom):
    return mapping(transform(Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform, geom))


def config(**changes):
    values = dict(target_epsg="32632", resolution=3, confidence_min=70,
                  clear_only=True, mask_classes=["snow", "shadow", "light_haze", "heavy_haze", "cloud"])
    values.update(changes)
    return values


def area(geometry=None, zones=None):
    geom = geometry or box(500001, 990, 500013, 1002)
    geo = geographic(geom)
    return dict(id="aoi", name="Test AOI", geometry=geo,
                zones=zones or [dict(id="zone", geometry=geo)])


def source(tmp_path, scene, values, *, confidence=90, cloud=None, shadow=None,
           unusable=None, acquired="2026-01-01T10:00:00Z", zone="zone", clear=None,
           image_transform=TRANSFORM):
    values = np.asarray(values, dtype=np.uint16)
    if values.ndim == 2:
        values = np.stack([values, values * 2])
    rows, cols = values.shape[1:]
    mask = np.zeros((8, rows, cols), dtype=np.uint8)
    mask[0] = 1
    mask[6] = confidence
    for positions, band in ((cloud, 5), (shadow, 2)):
        if positions is not None:
            mask[band][positions] = 1
            mask[0][positions] = 0
    if clear is not None:
        mask[0] = clear
    if unusable is not None:
        mask[7][unusable] = 4
    image_path = tmp_path / (scene + "_" + zone + "_image.tif")
    mask_path = tmp_path / (scene + "_" + zone + "_udm2.tif")
    with rasterio.open(image_path, "w", driver="GTiff", width=cols, height=rows,
                       count=values.shape[0], dtype="uint16", crs=CRS,
                       transform=image_transform, nodata=0) as ds:
        ds.write(values)
    with rasterio.open(mask_path, "w", driver="GTiff", width=cols, height=rows,
                       count=8, dtype="uint8", crs=CRS, transform=image_transform, nodata=0) as ds:
        ds.write(mask)
    return dict(scene_id=scene, acquired=acquired, date=acquired[:10], zone_id=zone,
                image_path=str(image_path), mask_path=str(mask_path), coreg_status="anchor",
                cloud_percent=0, clear_percent=100)


def read_day(summary, index=0):
    day = summary["dates"][index]
    with rasterio.open(day["files"]["mosaic"]) as ds:
        data = ds.read()
        grid = (ds.transform, ds.width, ds.height, ds.crs)
    with rasterio.open(day["files"]["provenance"]) as ds:
        provenance = ds.read(1)
    return data, provenance, grid


def test_masked_pixels_replaced_as_complete_band_vectors(tmp_path):
    bad = np.zeros((4, 4), dtype=bool)
    bad[0, 0] = True
    shadow = np.zeros((4, 4), dtype=bool)
    shadow[1, 1] = True
    unusable = np.zeros((4, 4), dtype=bool)
    unusable[2, 2] = True
    first = source(tmp_path, "A", np.full((4, 4), 10), confidence=95,
                   cloud=bad, shadow=shadow, unusable=unusable)
    second = source(tmp_path, "B", np.full((4, 4), 100), confidence=80)
    summary = process_area(area(), [first, second], config(), tmp_path / "out")
    data, provenance, _ = read_day(summary)
    fallback = bad | shadow | unusable
    assert data.shape == (2, 4, 4)
    np.testing.assert_array_equal(data[0], np.where(fallback, 100, 10))
    np.testing.assert_array_equal(data[1], data[0] * 2)
    np.testing.assert_array_equal(provenance, np.where(fallback, 2, 1))
    assert summary["dates"][0]["valid_percent_aoi_pixels"] == 100


def test_invalid_bright_cloud_never_contaminates_bilinear_pixels(tmp_path):
    values = np.full((4, 4), 10)
    cloud = np.zeros((4, 4), dtype=bool)
    cloud[:, 2:] = True
    values[cloud] = 30000
    record = source(tmp_path, "A", values, cloud=cloud)
    summary = process_area(area(), [record], config(resolution=2), tmp_path / "out")
    data, provenance, _ = read_day(summary)
    valid = provenance > 0
    assert valid.any() and (~valid).any()
    np.testing.assert_allclose(data[0][valid], 10, atol=1e-5)
    np.testing.assert_allclose(data[1][valid], 20, atol=1e-5)
    assert (data[:, ~valid] == NODATA).all()


def test_all_dates_identical_grid_and_utc_grouping(tmp_path):
    first = source(tmp_path, "A", np.full((4, 4), 10), acquired="2026-01-02T00:30:00+02:00")
    second = source(tmp_path, "B", np.full((4, 4), 20), acquired="2026-01-02T10:00:00Z")
    summary = process_area(area(), [second, first], config(), tmp_path / "out")
    assert [d["date"] for d in summary["dates"]] == ["2026-01-01", "2026-01-02"]
    assert read_day(summary, 0)[2] == read_day(summary, 1)[2]


def test_zones_and_polygon_holes_limit_sources(tmp_path):
    outer = box(500001, 990, 500013, 1002)
    hole = box(500004, 993, 500007, 996)
    geom = Polygon(outer.exterior.coords, [hole.exterior.coords])
    left = box(500001, 990, 500007, 1002)
    right = box(500007, 990, 500013, 1002)
    zones = [dict(id="left", geometry=geographic(left)), dict(id="right", geometry=geographic(right))]
    first = source(tmp_path, "A", np.full((4, 4), 10), zone="left", confidence=95)
    second = source(tmp_path, "B", np.full((4, 4), 20), zone="right", confidence=80)
    summary = process_area(area(geom, zones), [first, second, first], config(), tmp_path / "out")
    data, provenance, _ = read_day(summary)
    assert summary["duplicate_records_removed"] == 1
    assert (data[0, :, 2:] == 20).all()
    assert (data[0, :, :2][provenance[:, :2] > 0] == 10).all()
    assert data[0, 2, 1] == NODATA
    assert provenance[2, 1] == 0
    assert summary["dates"][0]["aoi_pixels"] == 15


def test_confidence_gate_and_all_invalid_reported(tmp_path):
    record = source(tmp_path, "A", np.full((4, 4), 10), confidence=60)
    summary = process_area(area(), [record], config(), tmp_path / "out")
    data, provenance, _ = read_day(summary)
    assert (data == NODATA).all() and not provenance.any()
    assert summary["dates"][0]["no_valid_pixels"] is True
    assert summary["valid_day_count"] == 0
    assert summary["warnings"]
    metadata = json.loads(Path(summary["dates"][0]["metadata_path"]).read_text())
    assert metadata["masking"]["before_resampling"] is True


def test_clear_precedes_higher_confidence_haze_and_ties_deterministic(tmp_path):
    first = source(tmp_path, "A", np.full((4, 4), 10), confidence=99)
    with rasterio.open(first["mask_path"], "r+") as ds:
        mask = ds.read()
        mask[0] = 0
        mask[3] = 1
        ds.write(mask)
    second = source(tmp_path, "B", np.full((4, 4), 20), confidence=71)
    summary = process_area(area(), [first, second], config(clear_only=False, mask_classes=[]), tmp_path / "out")
    assert (read_day(summary)[0][0] == 20).all()
    same_quality = source(tmp_path, "C", np.full((4, 4), 30), confidence=71)
    summary = process_area(area(), [same_quality, second], config(), tmp_path / "other")
    assert (read_day(summary)[0][0] == 20).all()


def test_missing_mask_unregistered_and_cancel_fail_clearly(tmp_path):
    record = source(tmp_path, "A", np.full((4, 4), 10))
    with pytest.raises(ValueError, match="UDM2 assente"):
        process_area(area(), [dict(record, mask_path="missing")], config(), tmp_path / "missing")
    with pytest.raises(ValueError, match="non coregistrata"):
        process_area(area(), [dict(record, coreg_status="failed")], config(), tmp_path / "failed")
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        process_area(area(), [record], config(), tmp_path / "cancelled", cancel=event)
    with pytest.raises(ValueError, match="proiettato"):
        process_area(area(), [record], config(target_epsg="4326"), tmp_path / "geographic")


def test_misaligned_udm_resampled_nearest_and_numeric_scale_preserved(tmp_path):
    record = source(tmp_path, "A", np.full((4, 4), 1234))
    mask = np.zeros((8, 2, 2), dtype=np.uint8)
    mask[0] = 1
    mask[6] = 90
    mask[0, 0, 1] = 0
    mask[5, 0, 1] = 1
    with rasterio.open(record["mask_path"], "w", driver="GTiff", width=2, height=2,
                       count=8, dtype="uint8", crs=CRS,
                       transform=from_origin(500001, 1002, 6, 6), nodata=0) as ds:
        ds.write(mask)
    with rasterio.open(record["image_path"], "r+") as ds:
        ds.scales = (0.0001, 0.0001)
    summary = process_area(area(), [record], config(), tmp_path / "out")
    data, provenance, _ = read_day(summary)
    assert (provenance[:2, 2:] == 0).all()
    assert (data[0][provenance > 0] == 1234).all()
    with rasterio.open(summary["dates"][0]["files"]["mosaic"]) as ds:
        assert ds.scales == (0.0001, 0.0001)


def test_more_than_one_processing_block(tmp_path):
    geom = box(500001, 996, 501543, 1002)
    values = np.full((2, 514), 10)
    record = source(tmp_path, "A", values)
    summary = process_area(area(geom), [record], config(), tmp_path / "out")
    data, provenance, _ = read_day(summary)
    assert data.shape == (2, 2, 514)
    assert (data[0] == 10).all() and (provenance == 1).all()
