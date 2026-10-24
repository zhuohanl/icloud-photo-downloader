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

    def test_xmp_disabled_by_default_to_avoid_metadata_crash(self):
        command = self.command('--download', '--all', '--include-edits')
        self.assertNotIn('--xmp-sidecar', command)
        self.assertIn('original', command)
        self.assertIn('adjusted', command)

    def test_xmp_can_be_explicitly_enabled(self):
        self.assertIn('--xmp-sidecar', self.command('--xmp-sidecar'))

    def test_missing_ssd_rejected(self):
        with patch.object(backup.sys, 'platform', 'darwin'), patch('pathlib.Path.is_mount', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '未挂载'):
                backup.validate_destination(Path('/Volumes/T7/test'))

    def test_preview_command_never_installs_or_authenticates(self):
        with patch.object(backup, 'runner', side_effect=AssertionError('must not start')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(backup.main(['--show-command', '--all']), 0)

    def test_connection_log_detected_despite_zero_exit(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code, failed = backup.run_once([sys.executable, '-c',
                "print('Cannot connect to Apple iCloud service')"])
        self.assertEqual(code, 0)
        self.assertTrue(failed)

    def test_connection_retry_then_success(self):
        with patch.object(backup, 'run_once', side_effect=[(0, True), (0, False)]) as run, \
             patch.object(backup, 'validate_destination') as validate, \
             patch.object(backup.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(backup.run_with_retries(['tool'], Path('/test'), 3), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(validate.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_connection_exhaustion_is_failure(self):
        with patch.object(backup, 'run_once', return_value=(0, True)) as run, \
             patch.object(backup, 'validate_destination'), patch.object(backup.time, 'sleep'), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(backup.run_with_retries(['tool'], Path('/test'), 2), 75)
        self.assertEqual(run.call_count, 3)

    def test_non_network_error_not_retried(self):
        with patch.object(backup, 'run_once', return_value=(1, False)) as run, \
             patch.object(backup, 'validate_destination'):
            self.assertEqual(backup.run_with_retries(['tool'], Path('/test'), 3), 1)
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
