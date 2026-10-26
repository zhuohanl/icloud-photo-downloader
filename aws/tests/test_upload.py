import base64
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import upload_archive as upload


class UploadTests(unittest.TestCase):
    def test_single_and_multipart_checksums_differ_from_full_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'file'
            path.write_bytes(b'abcdef')
            sha = hashlib.sha256(b'abcdef').hexdigest()
            self.assertEqual(upload.checksums(path, sha)[1], base64.b64encode(bytes.fromhex(sha)).decode())
            with patch.object(upload, 'PART_SIZE', 4):
                composite = hashlib.sha256(hashlib.sha256(b'abcd').digest() + hashlib.sha256(b'ef').digest()).digest()
                self.assertEqual(upload.checksums(path, sha)[1], base64.b64encode(composite).decode() + '-2')
            with self.assertRaises(RuntimeError):
                upload.checksums(path, '0' * 64)

    def test_manifest_rejects_hidden_traversal_and_external_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for target in ('media/../secret', '/etc/passwd', 'media/2026/01/._image', 'cookies/token'):
                with self.assertRaises(ValueError):
                    upload.safe_path(root, target)
            (root / 'media').mkdir()
            (root / 'media/file').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                upload.safe_path(root, 'media/file')

    def test_multipart_file_included_in_sample(self):
        rows = [{'bytes': 1}, {'bytes': 2}, {'bytes': upload.PART_SIZE + 1}]
        self.assertEqual(upload.select_sample(rows, 2), [rows[0], rows[2]])

    def test_existing_object_mismatch_is_not_overwritten(self):
        class Client:
            def head_object(self, **kwargs):
                return {'ContentLength': 3, 'ChecksumSHA256': 'wrong'}
        class Transfer:
            def upload_file(self, *args, **kwargs):
                raise AssertionError('Existing objects must not be overwritten')
        config = {'bucket': 'bucket', 'expected_account': '0' * 12, 'storage_class': 'INTELLIGENT_TIERING'}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'file'
            path.write_bytes(b'abc')
            uploader = upload.ArchiveUploader(Client(), Transfer(), config)
            with self.assertRaises(RuntimeError):
                uploader.upload_one(path, 'key', hashlib.sha256(b'abc').hexdigest(), True)

    def test_account_and_prefix_are_required(self):
        config = {'region': 'ap-southeast-2', 'storage_class': 'INTELLIGENT_TIERING',
                  'bucket': 'personal-archive-ap-southeast-2-test1234', 'expected_account': '0' * 12,
                  'library_prefix': 'photos/library/v1/', 'snapshots_prefix': 'photos/snapshots/',
                  'snapshot_date': '2026-10-24'}
        upload.validate_config(config)
        for field, value in [('expected_account', ''), ('library_prefix', 'other/'),
                             ('region', 'us-east-1'), ('storage_class', 'STANDARD')]:
            with self.assertRaises(ValueError):
                upload.validate_config(dict(config, **{field: value}))

    def test_retries_only_connection_and_service_failures(self):
        self.assertTrue(upload.transient_error(upload.EndpointConnectionError(endpoint_url='https://s3.example')))
        for code, status, retry in [('AccessDenied', 403, False), ('SlowDown', 503, True), ('BadDigest', 400, False)]:
            error = upload.ClientError({'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'PutObject')
            self.assertEqual(upload.transient_error(error), retry)
        self.assertFalse(upload.transient_error(RuntimeError('checksum mismatch')))
