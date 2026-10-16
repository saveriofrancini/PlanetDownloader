"""Confirmed orders, crash-safe reconciliation, raw downloads, raster processing."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .api import AmbiguousSubmission, DownloadExpired, PlanetError, safe_message


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def safe_path(root, relative):
    """Reject POSIX traversal, Windows ADS/reserved names, and existing symlinks."""
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise PlanetError('Unsafe filename received.')
    raw_parts = relative.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{x}' for x in range(1, 10)), *(f'LPT{x}' for x in range(1, 10))}
    if any(not p or p in ('.', '..') or p.endswith((' ', '.')) or
           any(c in p for c in '<>:"|?*\x00') or any(ord(c) < 32 for c in p) or
           p.split('.')[0].upper() in reserved for p in raw_parts):
        raise PlanetError('Unsafe file path received.')
    root = Path(root).resolve()
    target = root.joinpath(*PurePosixPath(relative).parts)
    try:
        target.resolve().relative_to(root)
    except ValueError:
        raise PlanetError('File path is outside the job folder.') from None
    return target


def _relative_name(name, order_id):
    prefix = str(order_id) + '/'
    return name[len(prefix):] if name.startswith(prefix) else name


def _order_matches(order, payload):
    # The response has extra metadata; every requested field must still agree.
    for key, value in payload.items():
        if key == 'products':
            def normalize(products):
                normalized = []
                for product in products or []:
                    product = dict(product)
                    if 'item_ids' in product:
                        product['item_ids'] = sorted(product['item_ids'])
                    normalized.append(product)
                return sorted(normalized, key=lambda p: json.dumps(p, sort_keys=True))
            if normalize(order.get(key)) != normalize(value):
                return False
        elif key == 'tools':
            # Planet executes a fixed toolchain regardless of JSON array order.
            normalize_tools = lambda tools: sorted(json.dumps(tool, sort_keys=True) for tool in (tools or []))
            if normalize_tools(order.get(key)) != normalize_tools(value):
                return False
        elif order.get(key) != value:
            return False
    return True


def reconcile(client, payload):
    """Find an exact existing request; do not infer absence from a network failure."""
    matches = []
    for item in client.list_orders():
        client.check_cancel()
        if item.get('name') != payload['name']:
            continue
        detail = client.get_order(item['id'])
        if _order_matches(detail, payload):
            matches.append(detail)
        else:
            raise PlanetError('An order with the same name has different parameters. Check your Planet account.')
    if len(matches) > 1:
        raise PlanetError('Multiple orders match the same submission. Check the identifiers on Planet before resuming.')
    return matches[0] if matches else None


def _get_or_create(client, payload, entry, save, emit):
    if entry.get('order_id'):
        return client.get_order(entry['order_id'])
    if entry.get('submission') in ('intent', 'ambiguous'):
        emit('Reconciling a previous submission with an uncertain outcome…')
        order = reconcile(client, payload)
        if order:
            entry.update(order_id=order['id'], submission='accepted')
            save()
            return order
        entry['submission'] = 'ambiguous'
        save()
        raise AmbiguousSubmission(
            'A previous submission has an uncertain outcome and is not yet in the Planet order list. '
            'No duplicate order was created. Wait and use Resume; if this persists, '
            f'check order «{payload["name"]}» on Planet. Orders older than three months '
            'may require Planet support. Do not start a new search to bypass this check.')
    entry.update(submission='intent', name=payload['name'], payload_hash=fingerprint(payload),
                 intent_at=datetime.now(timezone.utc).isoformat())
    save()  # Must reach disk BEFORE the potentially chargeable POST.
    try:
        order = client.create_order(payload)
    except AmbiguousSubmission:
        entry['submission'] = 'ambiguous'
        save()
        order = reconcile(client, payload)
        if order is None:
            raise AmbiguousSubmission(
                'Planet may have accepted the order, but it is not yet visible. No automatic resubmission. '
                f'Use Resume later and check the name «{payload["name"]}» on Planet.') from None
    except PlanetError:
        # A definite HTTP rejection is safe to try again after explicit resume confirmation.
        entry['submission'] = 'rejected'
        save()
        raise
    entry.update(order_id=order['id'], submission='accepted')
    save()
    return order


def _poll(client, order, entry, save, emit):
    last = None
    started = last_progress = time.monotonic()
    while True:
        client.check_cancel()
        status = order.get('state', 'unknown')
        entry['remote_state'] = status
        save()
        now = time.monotonic()
        if status != last:
            emit(f'Order {order["id"]}: {status}.')
            last = status
            last_progress = now
        if status in {'success', 'partial', 'failed', 'cancelled'}:
            return order
        if status not in {'queued', 'running', 'initializing', 'processing'}:
            raise PlanetError(f'Unrecognized Planet state: {safe_message(status)}.')
        if now - last_progress >= 60:
            emit(f'Order {order["id"]}: still {status}, '
                 f'waiting for {int(now - started)} s.')
            last_progress = now
        client.wait(getattr(client, 'poll_interval', 10))
        order = client.get_order(order['id'])


def _result_map(order):
    results = order.get('_links', {}).get('results') or order.get('results') or []
    mapped = {}
    for result in results:
        if result.get('delivery') == 'failed' or not result.get('name') or not result.get('location'):
            continue
        name = _relative_name(result['name'], order['id'])
        if name in mapped:
            raise PlanetError('Planet returned two files with the same path.')
        mapped[name] = result
    return mapped


def _download_order(client, order, folder, entry, save, emit):
    folder.mkdir(parents=True, exist_ok=True)
    downloads = entry.setdefault('files', {})
    result_map = _result_map(order)

    def download_one(name, expected=None):
        nonlocal order, result_map
        path = safe_path(folder, name)
        for refresh in range(2):
            result = result_map.get(name)
            if result is None:
                raise PlanetError(f'Missing delivery link for {name}.')
            try:
                info = client.download(result['location'], path, expected, downloads.get(name))
                downloads[name] = info
                save()
                return path
            except DownloadExpired:
                if refresh:
                    raise
                emit('Refreshing expired download links…')
                order = client.get_order(order['id'])
                result_map = _result_map(order)

    manifest_names = [n for n in result_map if PurePosixPath(n).name == 'manifest.json']
    if len(manifest_names) != 1:
        raise PlanetError('Order manifest missing or ambiguous: resume the job later.')
    manifest_name = manifest_names[0]
    manifest_path = download_one(manifest_name)
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest_files = manifest['files']
        if not isinstance(manifest_files, list):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise PlanetError('Invalid Planet manifest.') from None
    base = PurePosixPath(manifest_name).parent
    manifest_by_name = {}
    for item in manifest_files:
        name = str(base / item['path'])
        safe_path(folder, name)
        if name in manifest_by_name:
            raise PlanetError('Duplicate paths in the Planet manifest.')
        manifest_by_name[name] = item
    # Retain every delivered asset, including sidecars and files not annotated in the manifest.
    all_names = sorted(set(result_map) | set(manifest_by_name))
    files = []
    for index, name in enumerate(all_names, 1):
        client.check_cancel()
        if name == manifest_name:
            continue
        emit(f'Checking/downloading file {index}/{len(all_names)}: {PurePosixPath(name).name}')
        info = manifest_by_name.get(name, {})
        path = download_one(name, info)
        files.append({'path': str(path), 'relative': name, 'annotations': info.get('annotations') or {}})
    entry['download_complete'] = True
    save()
    return files


def _records_for_order(area, spec, files, config):
    from .catalog import PRODUCTS
    product = PRODUCTS[config['product']]
    records, failures, warnings = [], [], []
    scenes = {scene['id']: scene for scene in area['scenes']}
    for scene_id in spec['item_ids']:
        images, masks, qualities = [], [], []
        for file in files:
            annotation = file['annotations']
            basename = Path(file['path']).name.lower()
            annotated_id = annotation.get('planet/item_id')
            if annotated_id != scene_id and (annotated_id or not basename.startswith(scene_id.lower() + '_')):
                continue
            suffix = Path(file['path']).suffix.lower()
            asset_type = annotation.get('planet/asset_type', '')
            if suffix in ('.tif', '.tiff'):
                if asset_type == product['mask_asset'] or 'udm2' in basename:
                    masks.append(file['path'])
                elif asset_type == product['asset'] or (not asset_type and 'analytic' in basename and 'udm' not in basename):
                    images.append(file['path'])
            elif suffix == '.json' and 'coreg' in basename:
                qualities.append(file['path'])
        if len(images) != 1 or len(masks) != 1:
            failures.append({'scene_id': scene_id, 'zone_id': spec['zone_id'], 'reason': 'Image or UDM2 missing or ambiguous in the delivery.'})
            continue
        is_anchor = scene_id == spec['anchor_id']
        if not is_anchor and not ('_coreg' in Path(images[0]).stem.lower() and '_coreg' in Path(masks[0]).stem.lower()):
            failures.append({'scene_id': scene_id, 'zone_id': spec['zone_id'], 'reason': 'Coregistration failed: assets retained but excluded from mosaics.'})
            continue
        scene = scenes[scene_id]
        record = {key: scene.get(key) for key in ('acquired', 'date', 'cloud_percent', 'clear_percent')}
        record.update(scene_id=scene_id, zone_id=spec['zone_id'], image_path=images[0], mask_path=masks[0],
                      coreg_status='anchor' if is_anchor else 'coregistered')
        if qualities:
            record['quality_path'] = sorted(qualities)[0]
        elif not is_anchor:
            warnings.append(f'{scene_id}: coregistration quality report not found; image and mask both have _coreg suffixes.')
        records.append(record)
    return records, failures, warnings


def process_plan(client, plan, job_dir, emit, cancel):
    """Execute a stored plan only from the GUI confirmation handler."""
    from . import raster
    job_dir = Path(job_dir).resolve()
    plan_path, state_path = job_dir / 'plan.json', job_dir / 'state.json'
    if not plan_path.is_file():
        raise PlanetError('Saved plan missing. Repeat the search and confirmation in the GUI.')
    saved_plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if fingerprint(saved_plan) != fingerprint(plan):
        raise PlanetError('The plan changed after the summary; repeat the search and confirmation.')
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {
        'version': 1, 'plan_hash': fingerprint(plan), 'orders': {}, 'confirmed': True}
    if state.get('plan_hash') != fingerprint(plan):
        raise PlanetError('Resume state is incompatible with the saved plan.')
    state.setdefault('orders', {})
    def save():
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(state_path, state)
    save()
    summary = {'status': 'complete', 'job_dir': str(job_dir), 'areas': [],
               'warnings': list(plan.get('warnings', [])), 'failed': [], 'outputs': []}
    for area in plan['areas']:
        client.check_cancel()
        emit(f'Area: {area["name"]}. Processing {len(area["orders"])} orders.')
        by_pair, candidate_failures = {}, []
        for spec in area['orders']:
            client.check_cancel()
            state_key = area['id'] + ':' + spec['key']
            entry = state['orders'].setdefault(state_key, {'files': {}})
            payload = spec['payload']
            if entry.get('payload_hash') and entry['payload_hash'] != fingerprint(payload):
                raise PlanetError('Order parameters differ from the saved resume state.')
            try:
                order = _get_or_create(client, payload, entry, save, emit)
                order = _poll(client, order, entry, save, emit)
                if order['state'] in {'failed', 'cancelled'}:
                    raise PlanetError(f'Order {order["id"]} {order["state"]}: {safe_message(order.get("error_hints", []))}')
                if order['state'] == 'partial':
                    summary['warnings'].append(f'Order {order["id"]}: partial Planet delivery.')
                    summary['status'] = 'partial'
                folder = safe_path(job_dir / 'orders', area['id'] + '_' + spec['key'])
                files = _download_order(client, order, folder, entry, save, emit)
                records, failures, warnings = _records_for_order(area, spec, files, plan['config'])
                candidate_failures.extend(failures)
                summary['warnings'].extend(warnings)
                for record in records:
                    pair = (record['zone_id'], record['scene_id'])
                    by_pair.setdefault(pair, record)
                entry.pop('error', None)
                save()
            except (InterruptedError, AmbiguousSubmission):
                raise
            except (PlanetError, OSError, ValueError) as exc:
                error = safe_message(exc)
                emit(f'Incomplete order: {error}')
                entry['error'] = error
                save()
                summary['failed'].append({'area_id': area['id'], 'order_key': spec['key'], 'reason': error})
        # Explicitly account for every expected (zone, scene), including missing deliveries.
        expected = {(z['id'], scene_id) for z in area['zones'] for scene_id in z['scene_ids']}
        reasons = {(f['zone_id'], f['scene_id']): f['reason'] for f in candidate_failures}
        missing = expected - set(by_pair)
        for zone_id, scene_id in sorted(missing):
            reason = reasons.get((zone_id, scene_id), 'Scene not delivered or download incomplete.')
            emit(f'EXCLUDED {scene_id}, zone {zone_id}: {reason}')
            summary['failed'].append({'area_id': area['id'], 'zone_id': zone_id, 'scene_id': scene_id, 'reason': reason})
        records = [by_pair[key] for key in sorted(by_pair)]
        try:
            area_result = raster.process_area(area, records, plan['config'],
                safe_path(job_dir / 'areas', area['id']), emit, cancel)
            summary['areas'].append(area_result)
            summary['outputs'].extend(area_result.get('outputs', []))
            summary['warnings'].extend(area_result.get('warnings', []))
            summary['failed'].extend(area_result.get('failed', []))
            if area_result.get('status') == 'partial':
                summary['status'] = 'partial'
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            error = safe_message(exc)
            emit(f'Area processing incomplete: {error}')
            summary['failed'].append({'area_id': area['id'], 'reason': error})
    if summary['failed']:
        summary['status'] = 'partial'
    summary['warnings'] = list(dict.fromkeys(summary['warnings']))
    atomic_json(job_dir / 'summary.json', summary)
    state['status'] = summary['status']
    save()
    emit('Job complete.' if summary['status'] == 'complete' else
         'PARTIAL job: review excluded scenes and resume incomplete orders.')
    return summary
