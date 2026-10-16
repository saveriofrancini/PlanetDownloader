"""Planet client supporting API keys or an in-memory requests AuthBase.

An OAuth authenticator can manage token refresh without exposing tokens to this
client. API authentication never accompanies delivery URLs.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

API = 'https://api.planet.com'
ORDERS = API + '/compute/ops/orders/v2'


class PlanetError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class AmbiguousSubmission(PlanetError):
    """The server may have accepted a create request: never blindly retry."""


class DownloadExpired(PlanetError):
    pass


def safe_message(value, secret=''):
    value = str(value)
    if secret:
        value = value.replace(secret, '[redacted]')
    value = re.sub(r'\bBearer\s+[^\s\"\'<>]+', 'Bearer [redacted]', value, flags=re.IGNORECASE)
    value = re.sub(r'https?://[^\s\"\'<>]+', '[URL redacted]', value)
    return value[:800]


def validate_url(url, api=False):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname and
                 not parsed.username and not parsed.password and parsed.port in (None, 443))
        if api:
            valid = valid and parsed.hostname == 'api.planet.com'
        else:
            host = (parsed.hostname or '').lower()
            valid = valid and host != 'localhost' and not host.endswith('.localhost')
            try:
                valid = valid and ipaddress.ip_address(host).is_global
            except ValueError:
                pass
        if not valid:
            raise ValueError
    except ValueError:
        raise PlanetError('Invalid or disallowed service URL.') from None
    return url


def file_digest(path, algorithm='sha256'):
    digest = hashlib.new(algorithm)
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _transport_options(url):
    """Honor configured networking without enabling implicit .netrc credentials.

    ``trust_env=False`` also disables Requests' normal proxy/CA handling. Restore
    those transport settings explicitly; authentication remains under our control.
    Re-evaluate per destination so NO_PROXY also works for delivery redirects.
    """
    return {
        'proxies': requests.utils.get_environ_proxies(url),
        'verify': os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE') or True,
    }


def verify_file(path, expected=None, previous=None):
    """Existing files count as complete only if a known checksum matches."""
    path = Path(path)
    if not path.is_file():
        return False
    expected, previous = expected or {}, previous or {}
    size = expected.get('size', previous.get('size'))
    if size is not None and path.stat().st_size != int(size):
        return False
    digests = expected.get('digests') or {}
    if digests.get('sha256'):
        return file_digest(path) == str(digests['sha256']).lower()
    if digests.get('md5'):
        return file_digest(path, 'md5') == str(digests['md5']).lower()
    return bool(previous.get('sha256') and file_digest(path) == previous['sha256'])


class PlanetClient:
    def __init__(self, api_key=None, emit=lambda message: None, cancel=None, *, request_auth=None):
        """Use either an API key or a requests AuthBase, never both.

        ``request_auth`` supports the official Planet OAuth request authenticator;
        its credentials and refresh state remain in its own in-memory context.
        The separate delivery session is always unauthenticated.
        """
        if request_auth is not None:
            if api_key is not None:
                raise PlanetError('Provide an API key or a Planet authenticator, not both.')
            if not isinstance(request_auth, requests.auth.AuthBase):
                raise PlanetError('Invalid Planet authenticator: requests.auth.AuthBase is required.')
            self._key = ''
            self._api_auth = request_auth
        else:
            if not isinstance(api_key, str) or not api_key.strip():
                raise PlanetError('Enter a Planet API key or authenticate with Planet.')
            self._key = api_key.strip()
            self._api_auth = (self._key, '')
        self.emit = emit
        self.cancel = cancel if cancel is not None else threading.Event()
        self.session = requests.Session()
        self.download_session = requests.Session()
        # Disable implicit .netrc authentication on both sessions.
        self.session.trust_env = False
        self.download_session.trust_env = False
        self.session.headers.update({'User-Agent': 'Planet-Local-Downloader/1.0'})
        self.download_session.headers.update({'User-Agent': 'Planet-Local-Downloader/1.0'})
        self.retry_count = 5
        self.poll_interval = 10
        # Completed AOI percentages only, scoped to this client/search in memory.
        self._coverage_cache = {}

    def close(self):
        self.session.close()
        self.download_session.close()

    def check_cancel(self):
        if self.cancel.is_set():
            raise InterruptedError('Local operation paused; submitted Planet orders continue.')

    def wait(self, seconds):
        if self.cancel.wait(seconds):
            self.check_cancel()

    def _request(self, method, url, *, json=None, params=None, retry=True, create=False):
        validate_url(url, api=True)
        attempts = self.retry_count if retry else 1
        for attempt in range(attempts):
            self.check_cancel()
            try:
                response = self.session.request(method, url, json=json, params=params,
                    auth=self._api_auth, timeout=(15, 90), allow_redirects=False,
                    **_transport_options(url))
            except requests.RequestException:
                if create:
                    raise AmbiguousSubmission('Order submission outcome is uncertain. Reconciliation is required.') from None
                if attempt + 1 == attempts:
                    raise PlanetError('Planet connection interrupted or timed out. Try again later.') from None
                self.wait(min(30, 2 ** attempt))
                continue
            status = response.status_code
            if create and (status >= 500 or status == 408):
                response.close()
                raise AmbiguousSubmission('Planet did not confirm order creation; the outcome is uncertain.', status)
            if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                delay = response.headers.get('Retry-After', '')
                try:
                    delay = min(60, max(1, float(delay)))
                except ValueError:
                    delay = min(30, 2 ** attempt)
                response.close()
                self.emit(f'Planet is temporarily busy (HTTP {status}); retrying.')
                self.wait(delay)
                continue
            if not 200 <= status < 300:
                # With external OAuth we deliberately never inspect token material,
                # so a server response echo cannot be reliably redacted against it.
                # Keep OAuth HTTP errors to status text rather than echoing a body.
                detail = safe_message(response.text, self._key) if self._key else ''
                response.close()
                prefix = {401: 'Invalid or expired Planet credentials', 403: 'Insufficient access or quota',
                          429: 'Request rate limit reached'}.get(status, 'Planet request rejected')
                raise PlanetError(f'{prefix} (HTTP {status}). {detail}', status)
            try:
                data = response.json()
            except ValueError:
                cls = AmbiguousSubmission if create else PlanetError
                raise cls('Invalid Planet response; no automatic resubmission.') from None
            finally:
                response.close()
            return data

    def search(self, item_type, filter_dict):
        result = self._request('POST', API + '/data/v1/quick-search',
            json={'item_types': [item_type], 'filter': filter_dict},
            params={'_page_size': 250, '_sort': 'acquired asc'})
        visited = set()
        while True:
            self.check_cancel()
            yield from result.get('features', [])
            link = result.get('_links', {}).get('_next')
            if not link:
                break
            link = urljoin(API, link)
            if link in visited:
                raise PlanetError('Repeated Planet pagination link; search stopped to avoid incomplete results.')
            visited.add(link)
            result = self._request('GET', link)

    def item_assets(self, item_type, item_id):
        return self._request('GET', API + '/data/v1/item-types/' + quote(item_type, safe='') +
                             '/items/' + quote(item_id, safe='') + '/assets')

    @staticmethod
    def _coverage_value(data, band):
        # Planet retains clear_percent as the response key for a selected band.
        value = data.get(f'{band}_percent', data.get('clear_percent'))
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise PlanetError('Planet did not return a valid UDM2 percentage.') from None
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise PlanetError('UDM2 percentage out of range.')
        return value

    @staticmethod
    def _coverage_request_info(item_type, item_id, geometry, band):
        if band not in {'clear', 'cloud', 'snow_ice', 'cloud_shadow', 'light_haze', 'heavy_haze'}:
            raise ValueError('Unsupported UDM2 band.')
        url = API + '/data/v1/item-types/' + quote(item_type, safe='') + '/items/' + quote(item_id, safe='') + '/coverage'
        geometry_hash = hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(',', ':'),
                                                  allow_nan=False).encode()).hexdigest()
        return url, (item_type, item_id, geometry_hash, band)

    def prime_coverage(self, item_type, item_id, geometry, band='clear', cancel=None):
        """Start UDM2 activation without polling; cache already-complete results.

        Returns the percentage when already complete, otherwise None. A later
        coverage() call for the same scene, geometry and band consumes the cache
        or performs normal polling. No credentials or delivery URLs are cached.
        """
        self.check_cancel()
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Search paused.')
        url, key = self._coverage_request_info(item_type, item_id, geometry, band)
        data = self._request('POST', url, params={'mode': 'UDM2', 'band': band}, json={'geometry': geometry})
        status = data.get('status')
        if status == 'complete':
            value = self._coverage_value(data, band)
            self._coverage_cache[key] = value
            return value
        if status == 'activating':
            return None
        raise PlanetError(f'UDM2 calculation incomplete: {safe_message(status)}.')

    def coverage(self, item_type, item_id, geometry, band='clear', cancel=None):
        self.check_cancel()
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Search paused.')
        url, key = self._coverage_request_info(item_type, item_id, geometry, band)
        if key in self._coverage_cache:
            return self._coverage_cache.pop(key)
        started = time.monotonic()
        last_progress = None
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Search paused.')
            data = self._request('POST', url, params={'mode': 'UDM2', 'band': band}, json={'geometry': geometry})
            status = data.get('status')
            if status == 'complete':
                return self._coverage_value(data, band)
            if status != 'activating':
                raise PlanetError(f'UDM2 calculation incomplete: {safe_message(status)}.')
            now = time.monotonic()
            elapsed = now - started
            if last_progress is None or now - last_progress >= 60:
                self.emit(f'Scene {item_id}: UDM2 activation for {band} in progress '
                          f'({int(elapsed)} s elapsed); waiting for Planet.')
                last_progress = now
            if elapsed > 3600:
                raise PlanetError('UDM2 activation exceeded one hour. Repeat the search later.')
            self.wait(self.poll_interval)

    def create_order(self, payload):
        data = self._request('POST', ORDERS, json=payload, retry=False, create=True)
        if not isinstance(data, dict) or not data.get('id'):
            raise AmbiguousSubmission('Order submitted but no identifier received.')
        return data

    def get_order(self, order_id):
        return self._request('GET', ORDERS + '/' + quote(order_id, safe=''))

    def list_orders(self):
        link, visited = ORDERS, set()
        while link:
            if link in visited:
                raise PlanetError('Repeated order pagination link.')
            visited.add(link)
            data = self._request('GET', link)
            yield from data.get('orders', [])
            nxt = data.get('_links', {}).get('_next') or data.get('_links', {}).get('next')
            link = urljoin(API, nxt) if nxt else None

    def download(self, url, destination, expected=None, previous=None):
        """Atomic, checksum-verified download. Resume at completed-file boundaries."""
        destination = Path(destination)
        if verify_file(destination, expected, previous):
            return {'size': destination.stat().st_size, 'sha256': file_digest(destination)}
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + '.part')
        expected = expected or {}
        for attempt in range(self.retry_count):
            self.check_cancel()
            response = None
            try:
                # Resolve redirects without ever introducing API or implicit credentials.
                current = validate_url(url)
                for _ in range(8):
                    response = self.download_session.get(current, stream=True, timeout=(15, 90),
                        allow_redirects=False, **_transport_options(current))
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = validate_url(urljoin(current, response.headers.get('Location', '')))
                        response.close()
                        continue
                    break
                else:
                    raise PlanetError('Too many download redirects.')
                if response.status_code in (401, 403):
                    raise DownloadExpired('Download link expired: refresh required.', response.status_code)
                if response.status_code != 200:
                    if response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < self.retry_count:
                        response.close()
                        self.wait(min(30, 2 ** attempt))
                        continue
                    raise PlanetError(f'Download rejected (HTTP {response.status_code}).', response.status_code)
                sha256, md5, size = hashlib.sha256(), hashlib.md5(), 0
                with part.open('wb') as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        self.check_cancel()
                        if chunk:
                            stream.write(chunk)
                            sha256.update(chunk)
                            md5.update(chunk)
                            size += len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected.get('size') is not None and size != int(expected['size']):
                    raise PlanetError('File size differs from the Planet manifest; file rejected.')
                content_length = response.headers.get('Content-Length')
                if content_length and not response.headers.get('Content-Encoding') and size != int(content_length):
                    raise PlanetError('Incomplete download; file rejected.')
                digests = expected.get('digests') or {}
                for algo, actual in [('sha256', sha256.hexdigest()), ('md5', md5.hexdigest())]:
                    if digests.get(algo) and str(digests[algo]).lower() != actual:
                        raise PlanetError('Checksum differs from the Planet manifest; file rejected.')
                os.replace(part, destination)
                return {'size': size, 'sha256': sha256.hexdigest()}
            except requests.RequestException:
                if attempt + 1 == self.retry_count:
                    raise PlanetError('Connection interrupted during download. Resume the job.') from None
                self.wait(min(30, 2 ** attempt))
            finally:
                if response is not None:
                    response.close()
        raise PlanetError('Download incomplete.')
