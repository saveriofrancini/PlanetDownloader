import io
import json
import threading
import time
import zipfile

import pytest

from planet_downloader.app import create_app
from planet_downloader.config import validate_config


@pytest.fixture
def app(tmp_path):
    application = create_app(tmp_path / 'uploads')
    application.config['TESTING'] = True
    return application


def auth(app):
    return {'X-App-Token': app.extensions['planet_token']}


def await_status(app, status):
    for _ in range(200):
        if app.extensions['planet_state']['status'] == status:
            return
        time.sleep(.01)
    pytest.fail(str(app.extensions['planet_state']))


def test_loopback_csrf_and_origin_protection(app):
    client = app.test_client()
    assert client.get('/').status_code == 200
    assert client.get('/api/info').status_code == 403
    assert client.get('/api/info', headers=auth(app)).status_code == 200
    assert client.get('/api/info', headers={**auth(app), 'Origin': 'https://outside.invalid'}).status_code == 403
    assert client.get('/', headers={'Host': 'outside.invalid'}).status_code == 403


def test_zip_traversal_rejected(app):
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, 'w') as archive:
        archive.writestr('../outside.geojson', '{}')
    blob.seek(0)
    response = app.test_client().post('/api/vector', data={'files': (blob, 'areas.zip')}, headers=auth(app))
    assert response.status_code == 400
    assert 'Percorso' in response.json['error']


def test_config_finite_and_secrets_excluded(tmp_path):
    valid = validate_config({'output_dir': str(tmp_path), 'api_key': 'not-exported'})
    assert 'api_key' not in valid
    with pytest.raises(ValueError):
        validate_config({'output_dir': str(tmp_path), 'resolution': float('nan')})
    with pytest.raises(ValueError):
        validate_config({'output_dir': str(tmp_path), 'target_epsg': '4326'})
    with pytest.raises(ValueError):
        validate_config({'output_dir': str(tmp_path), 'start_date': '2026-02-03', 'end_date': '2026-02-01'})


def test_search_never_orders_and_confirmation_is_single_use(app, tmp_path, monkeypatch):
    from planet_downloader import api, catalog, workflow
    calls = []
    release = threading.Event()
    monkeypatch.setattr(api, 'PlanetClient', lambda *a, **k: object())
    def search(client, areas, config, emit, cancel):
        return {'version': 1, 'config': config, 'totals': {'scene_count': 1}, 'warnings': [],
                'areas': [{**areas[0], 'scenes': [], 'zones': [], 'orders': [{'key': 'order'}]}]}
    def execute(*args):
        calls.append('order')
        release.wait(5)
        return {'status': 'complete', 'outputs': [], 'failed': []}
    monkeypatch.setattr(catalog, 'build_plan', search)
    monkeypatch.setattr(workflow, 'process_plan', execute)
    client = app.test_client()
    geo = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {},
        'geometry': {'type': 'Polygon', 'coordinates': [[[11,43],[11.001,43],[11.001,43.001],[11,43.001],[11,43]]]}}]}
    uploaded = client.post('/api/vector', data={'files': (io.BytesIO(json.dumps(geo).encode()), 'test.geojson')}, headers=auth(app))
    assert uploaded.status_code == 200
    response = client.post('/api/search', json={'api_key': 'test-key-not-real', 'vector_id': uploaded.json['vector_id'], 'config': {'output_dir': str(tmp_path)}}, headers=auth(app))
    assert response.status_code == 200
    await_status(app, 'ready')
    job = client.get('/api/job', headers=auth(app)).json
    assert job['plan']['totals']['scene_count'] == 1
    assert calls == []
    assert 'test-key-not-real' not in json.dumps(job)
    assert client.post('/api/confirm', json={'job_id': job['id']}, headers=auth(app)).status_code == 400
    assert client.post('/api/confirm', json={'job_id': 'stale', 'confirmed': True}, headers=auth(app)).status_code == 409
    confirmed = client.post('/api/confirm', json={'job_id': job['id'], 'confirmed': True}, headers=auth(app))
    assert confirmed.status_code == 200
    assert client.post('/api/confirm', json={'job_id': job['id'], 'confirmed': True}, headers=auth(app)).status_code == 409
    release.set()
    await_status(app, 'done')
    assert calls == ['order']
    plan = client.get('/api/plan', headers=auth(app))
    assert b'test-key-not-real' not in plan.data


def test_modified_resume_plan_rejected(app, tmp_path):
    (tmp_path / 'plan.json').write_text('{"version":1,"areas":[]}', encoding='utf8')
    (tmp_path / 'plan.sha256').write_text('incorrect')
    response = app.test_client().post('/api/resume', json={'job_dir': str(tmp_path), 'api_key': 'test-not-real'}, headers=auth(app))
    assert response.status_code == 400
    assert 'modificato' in response.json['error']
