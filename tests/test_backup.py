import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import icloud_backup as backup


class BackupTests(unittest.TestCase):
    def command(self, *flags):
        args = backup.arguments(['--username', 'test@example.com', *flags])
        return backup.download_arguments(args, Path('/Volumes/T7/test'), Path('/tmp/session'))

    def test_default_is_small_preview(self):
        command = self.command()
        self.assertIn('--dry-run', command)
        self.assertEqual(command[command.index('--recent') + 1], '10')
        self.assertNotIn('--library', command)

    def test_full_download_preserves_both_versions_without_deletes(self):
        command = self.command('--download', '--all', '--include-edits')
        self.assertNotIn('--dry-run', command)
        self.assertNotIn('--recent', command)
        self.assertEqual(command.count('--size'), 2)
        self.assertIn('original', command)
        self.assertIn('adjusted', command)
        for flag in ('--auto-delete', '--delete-after-download', '--keep-icloud-recent-days'):
            self.assertNotIn(flag, command)

    def test_shared_library(self):
        command = self.command('--library', 'Shared Library')
        self.assertEqual(command[command.index('--library') + 1], 'Shared Library')

    def test_missing_ssd_rejected(self):
        with patch.object(backup.sys, 'platform', 'darwin'), patch('pathlib.Path.is_mount', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '未挂载'):
                backup.validate_destination(Path('/Volumes/T7/test'))

    def test_preview_command_never_installs_or_authenticates(self):
        with patch.object(backup, 'runner', side_effect=AssertionError('must not start')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(backup.main(['--show-command', '--all']), 0)


if __name__ == '__main__':
    unittest.main()
