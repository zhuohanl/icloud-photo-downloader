#!/usr/bin/env python3
"""Run upstream source with bounded transport retries and secret-free diagnostics."""
import logging
import os
import sys
import time
from urllib.parse import urlsplit

import requests
from icloudpd import download
from icloudpd.cli import cli
from pyicloud_ipd.base import PyiCloudService
from pyicloud_ipd.exceptions import PyiCloudConnectionErrorException

LOG = logging.getLogger('icloudpd.transport')
SOURCE_CONNECT_TIMEOUT = 15
SOURCE_READ_TIMEOUT = 120


def retryable_read(method, url):
    return method.upper() in ('GET', 'HEAD') or (
        method.upper() == 'POST' and urlsplit(url).path.endswith(('/records/query', '/records/lookup'))
    )


def error_types(error):
    """Only class names from nested transport causes; never exception messages."""
    found = []
    pending = [error]
    seen = set()
    while pending and len(found) < 8:
        item = pending.pop(0)
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        found.append(type(item).__name__)
        pending += [item.__cause__, item.__context__, getattr(item, 'reason', None)]
        pending += [arg for arg in item.args if isinstance(arg, BaseException)]
    return '>'.join(found)


def install_transport_hooks():
    original_request = requests.Session.request
    original_init = PyiCloudService.__init__
    original_write = download.download_response_to_path

    def service_init(self, *args, **kwargs):
        # CLI does not expose its fixed 30s service timeout. Set explicit timeouts
        # without changing TLS certificate verification or authentication.
        kwargs.setdefault('http_timeout', (SOURCE_CONNECT_TIMEOUT, SOURCE_READ_TIMEOUT))
        original_init(self, *args, **kwargs)

    def request(self, method, url, **kwargs):
        safe_host = urlsplit(url).hostname or 'unknown'
        attempts = 4 if retryable_read(method, url) else 1
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = original_request(self, method, url, **kwargs)
                if response.status_code >= 400:
                    LOG.warning('HTTP_STATUS method=%s host=%s status=%s elapsed=%.1fs',
                                method, safe_host, response.status_code, time.monotonic() - started)
                elif time.monotonic() - started >= 10:
                    LOG.info('HTTP_SLOW method=%s host=%s elapsed=%.1fs',
                             method, safe_host, time.monotonic() - started)
                return response
            except (requests.Timeout, requests.ConnectionError) as error:
                # Never log str(error), full URLs, request headers, or bodies.
                LOG.warning('TRANSPORT_ERROR kind=%s method=%s host=%s elapsed=%.1fs attempt=%s/%s causes=%s',
                            type(error).__name__, method, safe_host,
                            time.monotonic() - started, attempt + 1, attempts, error_types(error))
                if attempt + 1 == attempts:
                    raise
                delay = (5, 15, 30)[attempt]
                LOG.warning('TRANSPORT_RETRY wait=%ss', delay)
                time.sleep(delay)

    def write_response(response, temp_download_path, append_mode, download_path, created_date):
        # Refuse to append a complete HTTP 200 body to a partial download.
        # Preserve the .part file for investigation rather than corrupting it.
        partial_size = os.path.getsize(temp_download_path) if append_mode else 0
        if partial_size > 0 and response.status_code != 206:
            response.close()
            raise PyiCloudConnectionErrorException(
                'Cannot connect to Apple iCloud service: server did not honor partial download Range')
        try:
            return original_write(response, temp_download_path, append_mode, download_path, created_date)
        except (requests.RequestException, TimeoutError) as error:
            LOG.warning('STREAM_ERROR kind=%s; partial file retained', type(error).__name__)
            raise PyiCloudConnectionErrorException(
                'Cannot connect to Apple iCloud service: interrupted media stream') from error
        finally:
            response.close()

    PyiCloudService.__init__ = service_init
    requests.Session.request = request
    download.download_response_to_path = write_response


def main():
    install_transport_hooks()
    return cli()


if __name__ == '__main__':
    sys.exit(main())
