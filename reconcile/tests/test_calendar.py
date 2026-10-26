import contextlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import organize_calendar as calendar
import reconcile_photos as reconcile


class CalendarTests(unittest.TestCase):
    def test_original_date_preferred_over_modification_time(self):
        item={'canonical_filesystem_mtime_ns':0}
        result=calendar.choose_date({'ExifIFD:DateTimeOriginal':'2025:12:31 23:59:00'},item,'Australia/Melbourne')
        self.assertEqual((result['year'],result['month']),(2025,12))
        self.assertEqual(result['basis'],'embedded_metadata')

    def test_video_original_timezone_date_preferred_over_render_time(self):
        item={'canonical_filesystem_mtime_ns':0}
        metadata={'Keys:CreationDate':'2025:01:31 23:50:00-05:00','QuickTime:CreateDate':'2025:02:01 15:50:00+11:00'}
        result=calendar.choose_date(metadata,item,'Australia/Melbourne')
        self.assertEqual((result['year'],result['month']),(2025,1))

    def test_bad_metadata_uses_file_time_and_is_flagged(self):
        item={'canonical_filesystem_mtime_ns':1761955200*10**9}
        result=calendar.choose_date({'ExifIFD:DateTimeOriginal':'0000:00:00 00:00:00'},item,'Australia/Melbourne')
        self.assertEqual(result['basis'],'filesystem_mtime_fallback')
        self.assertEqual(result['month'],11)

    def test_move_updates_manifest_and_preserves_content(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            root=base/'archive'; state=base/'state'; root.mkdir(); state.mkdir()
            old='media/iphone-only/photo__hash.jpg'
            path=root/old; path.parent.mkdir(parents=True); path.write_bytes(b'original-photo')
            digest=reconcile.digest(path)
            item=dict(sha256=digest,bytes=path.stat().st_size,target=old,kind='image',
                      canonical_filesystem_mtime_ns=path.stat().st_mtime_ns,sources=[])
            (root/'manifest.jsonl').write_text(json.dumps(item)+'\n')
            summary=dict(input_files=1,input_bytes=item['bytes'],unique_files=1,unique_bytes=item['bytes'],
                         duplicate_copies_removed=0,bytes_saved=0,iphone_only_media_files=1,
                         source_counts={'iphone':dict(files=1,bytes=item['bytes'])},destination=str(root))
            reconcile.atomic_json(root/'summary.json',summary)
            reconcile.atomic_json(root/'STATUS.json',dict(state='complete',unique_files=1,unique_bytes=item['bytes']))
            metadata={digest:{'ExifIFD:DateTimeOriginal':'2024:03:02 10:11:12'}}
            with patch.object(calendar,'extract_dates',return_value=metadata),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(calendar.execute(root,state,'unused','Australia/Melbourne',True),0)
            manifest=calendar.load_manifest(root/'manifest.jsonl')
            self.assertTrue(manifest[0]['target'].startswith('media/2024/03/'))
            self.assertEqual(reconcile.digest(root/manifest[0]['target']),digest)
            self.assertFalse((root/'media/iphone-only').exists())
            self.assertEqual(json.loads((root/'STATUS.json').read_text())['state'],'complete')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(calendar.execute(root,state,'unused','Australia/Melbourne',True),0)

    def test_path_guard(self):
        with self.assertRaises(RuntimeError):
            calendar.valid_relative('media/../../outside.jpg')

    def test_rebuild_preserves_calendar_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); phone=base/'phone'; phone.mkdir()
            (phone/'photo.jpg').write_bytes(b'photo')
            root=base/'archive'; state=base/'reconcile-state'; dates=base/'date-state'; dates.mkdir()
            command=['--source','iphone='+str(phone),'--destination',str(root),
                     '--state-directory',str(state),'--build']
            with contextlib.redirect_stdout(io.StringIO()):
                reconcile.main(command)
            item=calendar.load_manifest(root/'manifest.jsonl')[0]
            metadata={item['sha256']:{'ExifIFD:DateTimeOriginal':'2025:02:03 12:00:00'}}
            with patch.object(calendar,'extract_dates',return_value=metadata),contextlib.redirect_stdout(io.StringIO()):
                calendar.execute(root,dates,'unused','Australia/Melbourne',True)
                reconcile.main(command)
            rebuilt=calendar.load_manifest(root/'manifest.jsonl')
            self.assertTrue(rebuilt[0]['target'].startswith('media/2025/02/'))
            self.assertFalse((root/'media/iphone-only').exists())
            self.assertEqual(reconcile.digest(root/rebuilt[0]['target']),item['sha256'])


if __name__=='__main__':
    unittest.main()
