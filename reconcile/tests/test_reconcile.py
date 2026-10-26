import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import reconcile_photos as reconcile


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cloud = self.root / 'cloud'
        self.phone = self.root / 'phone'
        self.cloud.mkdir()
        self.phone.mkdir()
        self.destination = self.root / 'archive'
        self.state = self.root / 'state'

    def add(self, root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return reconcile.main(['--source', 'icloud='+str(self.cloud), '--source', 'iphone='+str(self.phone),
                '--destination', str(self.destination), '--state-directory', str(self.state), '--workers', '1', '--build'])

    def test_union_deduplicates_bytes_and_preserves_edits_live_parts_and_sidecars(self):
        self.add(self.cloud, '2026/04/IMG_001_ID.HEIC', b'original-photo')
        self.add(self.phone, 'IMG_001.HEIC', b'original-photo')
        self.add(self.phone, 'IMG_E001.HEIC', b'edited-photo')
        self.add(self.cloud, '2026/04/IMG_001_ID.MOV', b'live-video')
        self.add(self.phone, 'IMG_001.MOV', b'live-video')
        self.add(self.cloud, '2026/04/IMG_001_ID.HEIC.xmp', b'xmp')
        self.add(self.phone, 'IMG_001.AAE', b'edit-instructions')
        self.add(self.phone, '.DS_Store', b'ignore')
        self.assertEqual(self.run_build(), 0)
        summary = json.loads((self.destination / 'summary.json').read_text())
        self.assertEqual(summary['input_files'], 7)
        self.assertEqual(summary['unique_files'], 5)
        self.assertEqual(summary['duplicate_copies_removed'], 2)
        self.assertEqual(summary['iphone_only_media_files'], 1)
        manifest = [json.loads(line) for line in (self.destination / 'manifest.jsonl').read_text().splitlines()]
        for item in manifest:
            data = (self.destination / item['target']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
            self.assertIsInstance(item['canonical_filesystem_mtime_ns'], int)
            self.assertIsInstance(item['sources'][0]['filesystem_mtime_ns'], int)
        xmp = next(item for item in manifest if item['target'].endswith('.xmp'))
        self.assertEqual(len(xmp['sources'][0]['related_media_sha256']), 1)
        aae = next(item for item in manifest if item['target'].endswith('.aae'))
        self.assertEqual(len(aae['sources'][0]['related_media_sha256']), 2)
        self.assertEqual((self.phone / 'IMG_001.HEIC').read_bytes(), b'original-photo')
        self.assertFalse((self.destination / '_partial').exists())
        self.assertEqual(self.run_build(), 0)
        summary = json.loads((self.destination / 'summary.json').read_text())
        self.assertEqual(summary['reused_this_run'], 5)

    def test_existing_output_corruption_is_not_overwritten(self):
        self.add(self.phone, 'photo.JPG', b'original')
        self.run_build()
        manifest = json.loads((self.destination / 'manifest.jsonl').read_text().splitlines()[0])
        target = self.destination / manifest['target']
        target.write_bytes(b'corrupted')
        with self.assertRaisesRegex(RuntimeError, 'refusing overwrite'):
            self.run_build()
        self.assertEqual(target.read_bytes(), b'corrupted')
        self.assertEqual(json.loads((self.destination / 'STATUS.json').read_text())['state'], 'building')

    def test_refuses_unowned_destination(self):
        self.add(self.phone, 'a.jpg', b'photo')
        self.destination.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'not owned'):
            self.run_build()

    def test_incomplete_and_empty_sources_are_rejected(self):
        path = self.add(self.phone, 'a.part', b'incomplete')
        with self.assertRaisesRegex(RuntimeError, 'Unfinished'):
            self.run_build()
        path.unlink()
        self.add(self.phone, 'a.jpg', b'')
        with self.assertRaisesRegex(RuntimeError, 'Empty'):
            self.run_build()

    def test_same_filename_different_content_is_preserved(self):
        self.add(self.cloud, '2026/04/a.jpg', b'one')
        self.add(self.phone, 'a.jpg', b'two')
        self.run_build()
        summary = json.loads((self.destination / 'summary.json').read_text())
        self.assertEqual(summary['unique_files'], 2)
        self.assertEqual(summary['duplicate_copies_removed'], 0)

    def test_source_change_after_hash_is_detected(self):
        source = self.add(self.phone, 'a.jpg', b'one')
        records, _ = reconcile.inventory([('iphone', self.phone)])
        source.write_bytes(b'different')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            reconcile.digest(source, (records[0]['size'], records[0]['mtime']))


if __name__ == '__main__':
    unittest.main()
