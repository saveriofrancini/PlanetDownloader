import hashlib
import json
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from planet_downloader.api import AmbiguousSubmission, PlanetClient, PlanetError, verify_file
from planet_downloader.workflow import (_get_or_create, _poll, _records_for_order, atomic_json,
                                       fingerprint, process_plan, safe_path)


class Response:
    def __init__(self, status=200, data=None, content=b'', headers=None):
        self.status_code, self.data, self.content = status, data, content
        self.headers, self.text = headers or {}, 'test service response'
    def json(self):
        return self.data
    def close(self):
        pass
    def iter_content(self, chunk_size):
        yield self.content


def client():
    obj = PlanetClient('test-key-not-a-real-credential')
    obj.wait = Mock()
    return obj


def test_authenticated_pagination_rejects_other_host():
    obj = client()
    obj.session.request = Mock(return_value=Response(data={
        'features': [], '_links': {'_next': 'https://attacker.invalid/steal'}}))
    with pytest.raises(PlanetError, match='URL'):
        list(obj.search('PSScene', {}))
    assert obj.session.request.call_count == 1
    assert obj.session.request.call_args.kwargs['auth'] == ('test-key-not-a-real-credential', '')
    assert obj.session.request.call_args.kwargs['allow_redirects'] is False


def test_oauth_authenticator_is_used_only_for_api_requests(tmp_path):
    class InMemoryAuth(requests.auth.AuthBase):
        def __call__(self, request):
            request.headers['Authorization'] = 'Bearer test-only-token'
            return request
    auth = InMemoryAuth()
    obj = PlanetClient(request_auth=auth)
    obj.session.request = Mock(return_value=Response(data={'id': 'a'}))
    assert obj.get_order('a')['id'] == 'a'
    assert obj.session.request.call_args.kwargs['auth'] is auth
    assert obj.session.request.call_args.kwargs['allow_redirects'] is False
    assert obj._key == ''
    obj.download_session.get = Mock(return_value=Response(content=b'complete'))
    obj.download('https://storage.invalid/file', tmp_path / 'file.tif')
    assert 'auth' not in obj.download_session.get.call_args.kwargs
    assert obj.download_session.auth is None
    assert 'Authorization' not in obj.download_session.headers


def test_oauth_rejects_ambiguous_or_invalid_authentication():
    auth = requests.auth.AuthBase()
    with pytest.raises(PlanetError, match='non entrambi'):
        PlanetClient('api-key', request_auth=auth)
    with pytest.raises(PlanetError, match='AuthBase'):
        PlanetClient(request_auth=lambda request: request)
    with pytest.raises(PlanetError, match='autenticarsi'):
        PlanetClient()
    with pytest.raises(PlanetError, match='autenticarsi'):
        PlanetClient('  ')


def test_oauth_http_error_never_echoes_response_token():
    obj = PlanetClient(request_auth=requests.auth.AuthBase())
    response = Response(status=401)
    response.text = 'Bearer token-unknown-to-client; access_token=raw-token-echo'
    obj.session.request = Mock(return_value=response)
    with pytest.raises(PlanetError, match='Credenziali Planet') as error:
        obj.get_order('a')
    assert 'token-unknown-to-client' not in str(error.value)
    assert 'raw-token-echo' not in str(error.value)


def test_create_timeout_never_retries():
    obj = client()
    obj.session.request = Mock(side_effect=requests.Timeout('signed-secret-url'))
    with pytest.raises(AmbiguousSubmission) as error:
        obj.create_order({'name': 'test'})
    assert obj.session.request.call_count == 1
    assert 'signed-secret-url' not in str(error.value)


def test_safe_get_retries_rate_limit():
    obj = client()
    obj.session.request = Mock(side_effect=[Response(429), Response(data={'id': 'a'})])
    assert obj.get_order('a')['id'] == 'a'
    assert obj.session.request.call_count == 2


def test_proxy_and_ca_settings_work_without_implicit_credentials(monkeypatch, tmp_path):
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.invalid:8080')
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', str(tmp_path / 'campus-ca.pem'))
    obj = client()
    obj.session.request = Mock(return_value=Response(data={'id': 'a'}))
    obj.get_order('a')
    options = obj.session.request.call_args.kwargs
    assert options['proxies']['https'] == 'http://proxy.example.invalid:8080'
    assert options['verify'] == str(tmp_path / 'campus-ca.pem')
    assert options['auth'] == ('test-key-not-a-real-credential', '')
    assert obj.session.trust_env is False
    monkeypatch.setenv('NO_PROXY', 'storage.invalid')
    obj.download_session.get = Mock(return_value=Response(content=b'complete'))
    obj.download('https://storage.invalid/file', tmp_path / 'file.tif')
    delivery_options = obj.download_session.get.call_args.kwargs
    assert delivery_options['proxies'] == {}
    assert delivery_options['verify'] == str(tmp_path / 'campus-ca.pem')
    assert 'auth' not in delivery_options
    assert obj.download_session.trust_env is False


def test_coverage_polls_and_preserves_band_percent():
    obj = client()
    obj.session.request = Mock(side_effect=[Response(data={'status': 'activating'}),
                                           Response(data={'status': 'complete', 'clear_percent': 4})])
    assert obj.coverage('PSScene', 'id', {'type': 'Polygon'}, 'cloud') == 4
    assert obj.session.request.call_args.kwargs['params'] == {'mode': 'UDM2', 'band': 'cloud'}
    obj.wait.assert_called_with(10)


def test_coverage_progress_at_first_activation_then_at_most_once_per_minute(monkeypatch):
    messages = []
    obj = client()
    obj.emit = messages.append
    monkeypatch.setattr('planet_downloader.api.time.monotonic', Mock(side_effect=[0, 5, 15, 65]))
    obj.session.request = Mock(side_effect=[
        Response(data={'status': 'activating'}),
        Response(data={'status': 'activating'}),
        Response(data={'status': 'activating'}),
        Response(data={'status': 'complete', 'clear_percent': 4}),
    ])
    assert obj.coverage('PSScene', 'test-scene', {'type': 'Polygon'}, 'cloud') == 4
    assert len(messages) == 2
    assert all('test-scene' in message and 'cloud' in message for message in messages)
    assert '5 s trascorsi' in messages[0]
    assert '65 s trascorsi' in messages[1]
    assert [call.args for call in obj.wait.call_args_list] == [(10,), (10,), (10,)]


def test_prime_coverage_starts_activation_once_without_polling():
    obj = client()
    obj.session.request = Mock(return_value=Response(data={'status': 'activating'}))
    assert obj.prime_coverage('PSScene', 'scene', {'type': 'Polygon'}, 'cloud') is None
    assert obj.session.request.call_count == 1
    obj.wait.assert_not_called()
    assert obj._coverage_cache == {}


def test_prime_completed_percentage_cache_is_scoped_and_consumed():
    obj = client()
    geom = {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [0, 1], [0, 0]]]}
    obj.session.request = Mock(side_effect=[Response(data={'status': 'complete', 'clear_percent': 0}),
                                           Response(data={'status': 'complete', 'clear_percent': 3}),
                                           Response(data={'status': 'complete', 'clear_percent': 97}),
                                           Response(data={'status': 'complete', 'clear_percent': 8})])
    assert obj.prime_coverage('PSScene', 'scene', geom, 'cloud') == 0
    # Dictionary order does not affect a geometry's cache identity.
    assert obj.coverage('PSScene', 'scene', dict(reversed(list(geom.items()))), 'cloud') == 0
    assert obj.session.request.call_count == 1
    assert obj._coverage_cache == {}
    assert obj.coverage('PSScene', 'scene', geom, 'cloud') == 3
    assert obj.prime_coverage('PSScene', 'scene', geom, 'clear') == 97
    changed_geom = dict(geom, coordinates=[[[0, 0], [2, 0], [0, 1], [0, 0]]])
    assert obj.coverage('PSScene', 'scene', changed_geom, 'clear') == 8
    assert obj.coverage('PSScene', 'scene', geom, 'clear') == 97
    assert obj.session.request.call_count == 4


def test_prime_coverage_rejects_bad_state_or_percentage():
    obj = client()
    obj.session.request = Mock(side_effect=[Response(data={'status': 'failed'}),
                                           Response(data={'status': 'complete', 'clear_percent': 110})])
    with pytest.raises(PlanetError, match='failed'):
        obj.prime_coverage('PSScene', 'scene', {'type': 'Polygon'})
    with pytest.raises(PlanetError, match='fuori intervallo'):
        obj.prime_coverage('PSScene', 'scene', {'type': 'Polygon'})
    assert obj._coverage_cache == {}


def test_download_has_no_auth_checks_checksum_and_resumes(tmp_path):
    obj = client()
    data = b'checked satellite content'
    expected = {'size': len(data), 'digests': {'sha256': hashlib.sha256(data).hexdigest()}}
    obj.download_session.get = Mock(side_effect=[Response(302, headers={'Location': 'https://storage.invalid/file?token=private'}),
                                                 Response(content=data)])
    path = tmp_path / 'raster.tif'
    result = obj.download('https://api.planet.com/compute/ops/download/?token=private', path, expected)
    assert path.read_bytes() == data
    assert not path.with_name('raster.tif.part').exists()
    for call in obj.download_session.get.call_args_list:
        assert 'auth' not in call.kwargs
    assert obj.download_session.auth is None
    assert obj.download_session.trust_env is False
    assert verify_file(path, expected)
    assert obj.download('https://storage.invalid/file', path, expected, result) == result
    assert obj.download_session.get.call_count == 2


def test_checksum_failure_does_not_commit(tmp_path):
    obj = client()
    obj.download_session.get = Mock(return_value=Response(content=b'broken'))
    destination = tmp_path / 'result.tif'
    with pytest.raises(PlanetError, match='Checksum'):
        obj.download('https://storage.invalid/f', destination,
                     {'digests': {'sha256': hashlib.sha256(b'good').hexdigest()}})
    assert not destination.exists()


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'C:/outside', 'a\\b', 'a/../../c',
                                  'a/file:stream', 'CON.txt', 'dir/file.', 'a//b'])
def test_delivery_paths_cannot_escape_or_use_windows_special_names(tmp_path, path):
    with pytest.raises(PlanetError):
        safe_path(tmp_path, path)


def test_create_intent_persisted_and_ambiguous_never_resent():
    payload = {'name': 'deterministic-order', 'products': [], 'source_type': 'scenes'}
    entry, snapshots = {}, []
    obj = Mock()
    obj.create_order.side_effect = AmbiguousSubmission('unknown')
    obj.list_orders.return_value = []
    save = lambda: snapshots.append(json.loads(json.dumps(entry)))
    with pytest.raises(AmbiguousSubmission):
        _get_or_create(obj, payload, entry, save, lambda s: None)
    assert snapshots[0]['submission'] == 'intent'
    assert snapshots[0]['payload_hash'] == fingerprint(payload)
    with pytest.raises(AmbiguousSubmission):
        _get_or_create(obj, payload, entry, save, lambda s: None)
    assert obj.create_order.call_count == 1


def test_uncertain_submission_recovers_exact_existing_order():
    payload = {'name': 'deterministic-order', 'products': [], 'source_type': 'scenes'}
    obj = Mock()
    obj.list_orders.return_value = [{'name': payload['name'], 'id': 'order-id'}]
    obj.get_order.return_value = dict(payload, id='order-id', state='running')
    entry = {'submission': 'ambiguous'}
    result = _get_or_create(obj, payload, entry, lambda: None, lambda s: None)
    assert result['id'] == entry['order_id'] == 'order-id'
    obj.create_order.assert_not_called()


@pytest.mark.parametrize('terminal', ['success', 'partial', 'failed', 'cancelled'])
def test_order_poll_reports_wait_once_per_minute_and_stops_at_terminal(monkeypatch, terminal):
    monkeypatch.setattr('planet_downloader.workflow.time.monotonic',
                        Mock(side_effect=[0, 0, 10, 60, 70, 120]))
    obj = Mock(poll_interval=10)
    running = {'id': 'order-id', 'state': 'running'}
    completed = {'id': 'order-id', 'state': terminal}
    obj.get_order.side_effect = [running, running, running, completed]
    entry, messages, save = {}, [], Mock()

    assert _poll(obj, running, entry, save, messages.append) is completed

    assert messages == [
        'Ordine order-id: running.',
        'Ordine order-id: ancora running, attesa da 60 s.',
        f'Ordine order-id: {terminal}.',
    ]
    assert entry['remote_state'] == terminal
    assert save.call_count == obj.check_cancel.call_count == 5
    assert obj.get_order.call_count == obj.wait.call_count == 4
    assert all(call.args == (10,) for call in obj.wait.call_args_list)


def test_coreg_suffix_required_for_both_image_and_mask():
    area = {'scenes': [{'id': 'scene-a', 'date': '2026-01-01', 'acquired': '2026-01-01T10:00:00Z'}]}
    spec = {'anchor_id': 'different', 'zone_id': 'zone', 'item_ids': ['scene-a']}
    files = [
        {'path': '/tmp/scene-a_AnalyticMS_SR_coreg.tif', 'annotations': {'planet/item_id': 'scene-a', 'planet/asset_type': 'ortho_analytic_4b_sr'}},
        {'path': '/tmp/scene-a_udm2.tif', 'annotations': {'planet/item_id': 'scene-a', 'planet/asset_type': 'ortho_udm2'}},
    ]
    records, failures, warnings = _records_for_order(area, spec, files, {'product': 'ps_sr_4b'})
    assert not records and len(failures) == 1
    files[1]['path'] = '/tmp/scene-a_udm2_coreg.tif'
    records, failures, warnings = _records_for_order(area, spec, files, {'product': 'ps_sr_4b'})
    assert len(records) == 1 and not failures


def test_partial_order_never_reports_complete(tmp_path, monkeypatch):
    from planet_downloader import raster
    monkeypatch.setattr(raster, 'process_area', lambda *args: {'status': 'complete', 'outputs': [], 'warnings': []})
    plan = {'config': {'product': 'ps_sr_4b'}, 'areas': [{
        'id': 'area', 'name': 'Area', 'scenes': [{'id': 'scene', 'acquired': '2026-01-01T10:00:00Z', 'date': '2026-01-01'}],
        'zones': [{'id': 'zone', 'scene_ids': ['scene']}],
        'orders': [{'key': 'key', 'zone_id': 'zone', 'anchor_id': 'scene', 'item_ids': ['scene'],
                    'payload': {'name': 'order', 'products': [], 'source_type': 'scenes'}}]}]}
    atomic_json(tmp_path / 'plan.json', plan)
    obj = Mock()
    obj.create_order.return_value = {'id': 'order-id', 'state': 'partial', '_links': {'results': [
        {'name': 'order-id/manifest.json', 'location': 'https://storage.invalid/signed-private'}]}}
    def download(url, path, *args):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"files": []}', encoding='utf8')
        return {'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    obj.download.side_effect = download
    result = process_plan(obj, plan, tmp_path, lambda s: None, threading.Event())
    assert result['status'] == 'partial'
    assert any(f.get('scene_id') == 'scene' for f in result['failed'])
    assert 'signed-private' not in (tmp_path / 'state.json').read_text()
    assert 'signed-private' not in (tmp_path / 'summary.json').read_text()
    assert (tmp_path / 'orders' / 'area_key' / 'manifest.json').exists()
