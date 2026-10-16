"""Real catalog → orders/downloads → UDM2 → mosaics → resume, entirely offline."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as transform_geometry

from planet_downloader.api import PlanetClient
from planet_downloader.catalog import build_plan
from planet_downloader.config import validate_config
from planet_downloader.workflow import atomic_json, process_plan


class BytesResponse:
    status_code = 200

    def __init__(self, content):
        self.content = content
        self.headers = {'Content-Length': str(len(content))}

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        pass


class OfflineStorage:
    """No network implementation; the real client's checksum/download code runs."""
    auth = None
    trust_env = False

    def __init__(self):
        self.files = {}
        self.requests = []

    def get(self, url, **kwargs):
        assert 'auth' not in kwargs
        assert kwargs.get('allow_redirects') is False
        self.requests.append(url)
        return BytesResponse(self.files[url])


def _tiff_bytes(values, transform):
    with MemoryFile() as memory:
        with memory.open(driver='GTiff', height=values.shape[1], width=values.shape[2],
                         count=values.shape[0], dtype=str(values.dtype), crs='EPSG:32632',
                         transform=transform) as raster:
            raster.write(values)
        return memory.read()


class OfflinePlanet(PlanetClient):
    """Fake Planet service, real application interfaces and real download method."""
    def __init__(self, features, native_transform, cancel):
        super().__init__('TEST_ONLY_NEVER_A_REAL_API_KEY', cancel=cancel)
        self.features = features
        self.native_transform = native_transform
        self.download_session = OfflineStorage()
        self.orders = {}
        self.creates = []
        self.coverage_calls = []
        self.prime_calls = []
        self.search_calls = []
        self.asset_calls = []

    def search(self, item_type, filter_dict):
        assert item_type == 'PSScene'
        self.search_calls.append(deepcopy(filter_dict))
        yield from deepcopy(self.features)

    def item_assets(self, item_type, item_id):
        self.asset_calls.append(item_id)
        return {'ortho_analytic_4b_sr': {'_permissions': ['download']},
                'ortho_udm2': {'_permissions': ['download']}}

    def coverage(self, item_type, item_id, geometry, band='clear', cancel=None):
        self.coverage_calls.append((item_id, band))
        _, key = self._coverage_request_info(item_type, item_id, geometry, band)
        if key in self._coverage_cache:
            return self._coverage_cache.pop(key)
        cloud = 4 if item_id == 'scene_a' else 0
        return cloud if band == 'cloud' else 100 - cloud

    def prime_coverage(self, item_type, item_id, geometry, band='clear', cancel=None):
        self.prime_calls.append((item_id, band))
        _, key = self._coverage_request_info(item_type, item_id, geometry, band)
        cloud = 4 if item_id == 'scene_a' else 0
        value = cloud if band == 'cloud' else 100 - cloud
        self._coverage_cache[key] = value
        return value

    def create_order(self, payload):
        self.creates.append(deepcopy(payload))
        order_id = f'offline-order-{len(self.creates)}'
        anchor = next(tool['coregister']['anchor_item'] for tool in payload['tools'] if 'coregister' in tool)
        files, manifest = {}, {'files': []}

        def add(path, content, annotations):
            files[path] = content
            manifest['files'].append({'path': path, 'size': len(content),
                'digests': {'sha256': hashlib.sha256(content).hexdigest(),
                            'md5': hashlib.md5(content).hexdigest()},
                'annotations': annotations})

        for scene_id in payload['products'][0]['item_ids']:
            suffix = 'anchor' if scene_id == anchor else 'coreg'
            base = {'scene_a': 1000, 'scene_b': 2000, 'scene_c': 3000}[scene_id]
            values = np.stack([np.full((10, 10), base + band, dtype='uint16') for band in range(1, 5)])
            udm2 = np.zeros((8, 10, 10), dtype='uint8')
            udm2[0] = 1
            udm2[6] = {'scene_a': 99, 'scene_b': 80, 'scene_c': 95}[scene_id]
            if scene_id == 'scene_a':
                # Masked bright pixels must never leak into the final mosaic.
                values[:, 3:5, 4:6] = 60000
                udm2[0, 3:5, 4:6] = 0
                udm2[5, 3:5, 4:6] = 1
            annotation = {'planet/item_id': scene_id, 'planet/item_type': 'PSScene'}
            add(f'PSScene/{scene_id}_AnalyticMS_SR_clip_{suffix}.tif',
                _tiff_bytes(values, self.native_transform),
                dict(annotation, **{'planet/asset_type': 'ortho_analytic_4b_sr'}))
            add(f'PSScene/{scene_id}_udm2_clip_{suffix}.tif',
                _tiff_bytes(udm2, self.native_transform),
                dict(annotation, **{'planet/asset_type': 'ortho_udm2'}))
            feature = next(item for item in self.features if item['id'] == scene_id)
            add(f'PSScene/{scene_id}_metadata.json', json.dumps(feature['properties']).encode(), annotation)
            if scene_id != anchor:
                # Empty after-score is valid for a scene that was already aligned.
                quality = {'matching_score_before': 0.95, 'matching_score_after': '',
                           'projection_epsg_after': '', 'anchor_item': anchor}
                add(f'PSScene/{scene_id}_coregistration_quality.json', json.dumps(quality).encode(), annotation)
        files['manifest.json'] = json.dumps(manifest).encode()
        results = []
        for relative, content in files.items():
            url = f'https://offline-storage.invalid/{order_id}/{relative}?signature=TEST_ONLY_SIGNED_TOKEN'
            self.download_session.files[url] = content
            results.append({'name': f'{order_id}/{relative}', 'location': url, 'delivery': 'success'})
        result = dict(deepcopy(payload), id=order_id, state='success', _links={'results': results})
        self.orders[order_id] = result
        return deepcopy(result)

    def get_order(self, order_id):
        return deepcopy(self.orders[order_id])

    def list_orders(self):
        yield from deepcopy(list(self.orders.values()))


def test_catalog_to_download_to_masked_daily_mosaics_and_resume(tmp_path):
    cancel, messages = threading.Event(), []
    native_transform = from_origin(500010, 4980012, 3, 3)
    metric_geometry = box(500010, 4979982, 500040, 4980012)
    to_wgs84 = Transformer.from_crs('EPSG:32632', 'EPSG:4326', always_xy=True)
    geometry = mapping(transform_geometry(to_wgs84.transform, metric_geometry))
    area = {'id': 'test_area', 'name': 'Small forest plot', 'geometry': geometry, 'area_km2': 0.0009}
    features = []
    for scene_id, acquired in [('scene_a', '2026-06-01T09:00:00Z'),
                               ('scene_b', '2026-06-01T10:00:00Z'),
                               ('scene_c', '2026-06-02T10:00:00Z')]:
        features.append({'type': 'Feature', 'id': scene_id, 'geometry': geometry,
            'properties': {'acquired': acquired, 'cloud_cover': 0.04 if scene_id == 'scene_a' else 0,
                           'clear_percent': 96 if scene_id == 'scene_a' else 100,
                           'quality_category': 'standard', 'ground_control': True,
                           'sun_elevation': 55, 'view_angle': 3}})
    client = OfflinePlanet(features, native_transform, cancel)
    config = validate_config({'start_date': '2026-06-01', 'end_date': '2026-06-02',
        'output_dir': str(tmp_path), 'target_epsg': '32632', 'batch_size': 1})
    plan = build_plan(client, [area], config, messages.append, cancel)
    assert plan['totals']['scene_count'] == 3
    assert plan['totals']['day_count'] == 2
    assert plan['totals']['order_count'] == 2  # The fixed anchor is repeated in two batches.
    assert len(client.coverage_calls) == 6
    assert len(client.asset_calls) == 3
    assert len(client.search_calls) == 1
    assert client.creates == []  # Searching/planning cannot submit chargeable orders.
    assert plan['areas'][0]['zones'][0]['anchor_id'] == 'scene_b'
    job_dir = tmp_path / 'Planet_job_offline'
    atomic_json(job_dir / 'plan.json', plan)
    result = process_plan(client, plan, job_dir, messages.append, cancel)
    assert result['status'] == 'complete'
    assert result['failed'] == []
    assert len(client.creates) == 2
    area_result = result['areas'][0]
    assert area_result['scene_count'] == 3
    assert area_result['unique_zone_scene_records'] == 3
    assert area_result['input_records'] == 3  # Repeated anchor removed before raster processing.
    assert [day['date'] for day in area_result['dates']] == ['2026-06-01', '2026-06-02']
    assert all(day['valid_pixels'] == day['aoi_pixels'] == 100 for day in area_result['dates'])
    assert len(result['outputs']) == 2
    grids = []
    for day in area_result['dates']:
        with rasterio.open(day['files']['mosaic']) as dataset:
            grids.append((dataset.crs, dataset.transform, dataset.width, dataset.height))
            assert dataset.count == 4
            assert dataset.dtypes == ('float32',) * 4
            assert dataset.tags()['ACQUISITION_DATE_UTC'] == day['date']
            data = dataset.read()
            expected = np.stack([np.full((10, 10), (1000 if day['date'] == '2026-06-01' else 3000) + band,
                                         dtype='float32') for band in range(1, 5)])
            if day['date'] == '2026-06-01':
                for band in range(4):
                    expected[band, 3:5, 4:6] = 2001 + band
            np.testing.assert_allclose(data, expected, rtol=0, atol=0.01)
            assert not np.any(data == 60000)
        with rasterio.open(day['files']['valid_mask']) as valid:
            assert np.all(valid.read(1) == 1)
        with rasterio.open(day['files']['provenance']) as provenance:
            values = provenance.read(1)
            if day['date'] == '2026-06-01':
                assert np.count_nonzero(values == 1) == 96
                assert np.count_nonzero(values == 2) == 4
            else:
                assert np.all(values == 3)
        assert Path(day['metadata_path']).is_file()
    assert grids[0] == grids[1]
    assert grids[0][1] == native_transform
    raw_files = [path for path in (job_dir / 'orders').rglob('*') if path.is_file()]
    assert len(raw_files) == 16
    assert len([path for path in raw_files if path.suffix == '.tif']) == 8
    assert all(path.stat().st_size > 0 for path in raw_files)
    assert not list(job_dir.rglob('*.part'))
    for path in job_dir.rglob('*.json'):
        text = path.read_text(encoding='utf-8')
        assert 'TEST_ONLY_NEVER_A_REAL_API_KEY' not in text
        assert 'TEST_ONLY_SIGNED_TOKEN' not in text
        assert 'offline-storage.invalid' not in text
    download_count = len(client.download_session.requests)
    resumed = process_plan(client, plan, job_dir, messages.append, cancel)
    assert resumed['status'] == 'complete'
    assert len(client.creates) == 2
    assert len(client.download_session.requests) == download_count  # Every complete file passed checksum verification.
    assert not list(job_dir.rglob('*.part'))
