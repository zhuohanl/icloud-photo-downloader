import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

try:
    import requests
    from icloudpd.cli import cli
except ImportError:
    requests = None


@unittest.skipIf(requests is None, 'requires isolated upstream source environment')
class TransportTests(unittest.TestCase):
    def setUp(self):
        import network_backend as backend
        self.backend = backend
        self.original_request = requests.Session.request
        self.original_init = backend.PyiCloudService.__init__
        self.original_write = backend.download.download_response_to_path

    def tearDown(self):
        requests.Session.request = self.original_request
        self.backend.PyiCloudService.__init__ = self.original_init
        self.backend.download.download_response_to_path = self.original_write

    def test_read_timeout_retries_and_logs_no_signed_url(self):
        response = requests.Response()
        response.status_code = 200
        with patch.object(requests.Session, 'request', side_effect=[requests.ReadTimeout('SECRET'), response]) as request, \
             patch.object(self.backend.time, 'sleep') as sleep:
            self.backend.install_transport_hooks()
            with self.assertLogs('icloudpd.transport', level='WARNING') as log:
                actual = requests.Session().get('https://example.com/private?token=SECRET', timeout=(15, 120))
        self.assertIs(actual, response)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(5)
        self.assertIn('ReadTimeout', '\n'.join(log.output))
        self.assertNotIn('SECRET', '\n'.join(log.output))
        self.assertNotIn('/private', '\n'.join(log.output))

    def test_login_post_never_retried(self):
        with patch.object(requests.Session, 'request', side_effect=requests.ReadTimeout()) as request:
            self.backend.install_transport_hooks()
            with self.assertLogs('icloudpd.transport'), self.assertRaises(requests.ReadTimeout):
                requests.Session().post('https://example.com/signin')
        self.assertEqual(request.call_count, 1)

    def test_cloud_query_is_retryable(self):
        self.assertTrue(self.backend.retryable_read('POST', 'https://example.com/database/1/records/query'))
        self.assertFalse(self.backend.retryable_read('POST', 'https://example.com/database/1/records/modify'))

    def test_stream_failure_propagates_as_connection_failure(self):
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO()
        with patch.object(self.backend.download, 'download_response_to_path', side_effect=requests.ConnectionError('SECRET')):
            self.backend.install_transport_hooks()
            with self.assertLogs('icloudpd.transport'), self.assertRaises(self.backend.PyiCloudConnectionErrorException):
                self.backend.download.download_response_to_path(response, 'temp', False, 'dest', None)

    def test_partial_download_requires_range_response(self):
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO()
        self.backend.install_transport_hooks()
        with patch.object(self.backend.os.path, 'getsize', return_value=3), \
             self.assertRaisesRegex(self.backend.PyiCloudConnectionErrorException, 'Range'):
            self.backend.download.download_response_to_path(response, 'temp', True, 'dest', None)

    def test_empty_partial_allows_full_response(self):
        response = requests.Response()
        response.status_code = 200
        response.raw = io.BytesIO()
        with patch.object(self.backend.download, 'download_response_to_path', return_value=True) as write, \
             patch.object(self.backend.os.path, 'getsize', return_value=0):
            self.backend.install_transport_hooks()
            self.assertTrue(self.backend.download.download_response_to_path(response, 'temp', True, 'dest', None))
        write.assert_called_once()

    def test_service_uses_explicit_read_and_connect_timeouts(self):
        with patch.object(self.backend.PyiCloudService, '__init__', return_value=None) as init:
            self.backend.install_transport_hooks()
            self.backend.PyiCloudService('com', 'test@example.com', lambda: None)
        self.assertEqual(init.call_args.kwargs['http_timeout'], (15, 120))

    def response(self, status, retry_after=None):
        response = requests.Response()
        response.status_code = status
        response.raw = io.BytesIO()
        if retry_after is not None:
            response.headers['Retry-After'] = retry_after
        return response

    def test_503_read_waits_then_recovers(self):
        unavailable = self.response(503)
        available = self.response(200)
        with patch.object(requests.Session, 'request', side_effect=[unavailable, available]) as request, \
             patch.object(self.backend.time, 'sleep') as sleep:
            self.backend.install_transport_hooks()
            with self.assertLogs('icloudpd.transport'):
                actual = requests.Session().get('https://example.com/media')
        self.assertIs(actual, available)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(60)
        self.assertTrue(unavailable.raw.closed)

    def test_retry_after_is_respected(self):
        self.assertEqual(self.backend.service_delay(self.response(503, '600'), 0), 600)
        self.assertEqual(self.backend.service_delay(self.response(503, 'invalid'), 1), 120)
        self.assertIsNone(self.backend.service_delay(self.response(503, '7200'), 0))

    def test_retry_after_http_date_is_respected(self):
        with patch.object(self.backend, 'datetime') as clock:
            clock.now.return_value = self.backend.parsedate_to_datetime('Sun, 25 Oct 2026 00:00:00 GMT')
            response = self.response(503, 'Sun, 25 Oct 2026 00:10:00 GMT')
            self.assertEqual(self.backend.service_delay(response, 0), 600)

    def test_503_auth_post_not_replayed(self):
        unavailable = self.response(503)
        with patch.object(requests.Session, 'request', return_value=unavailable) as request, \
             patch.object(self.backend.time, 'sleep') as sleep:
            self.backend.install_transport_hooks()
            with self.assertLogs('icloudpd.transport'):
                self.assertIs(requests.Session().post('https://example.com/signin'), unavailable)
        request.assert_called_once()
        sleep.assert_not_called()

    def test_503_retry_budget_is_finite(self):
        with patch.object(requests.Session, 'request', side_effect=lambda *a, **kw: self.response(503)) as request, \
             patch.object(self.backend.time, 'sleep') as sleep:
            self.backend.install_transport_hooks()
            with self.assertLogs('icloudpd.transport'):
                self.assertEqual(requests.Session().get('https://example.com/media').status_code, 503)
        self.assertEqual(request.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 120, 240])
