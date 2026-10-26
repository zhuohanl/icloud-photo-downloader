#!/usr/bin/env python3
"""Move non-calendar archive media into YYYY/MM; preserve content and provenance."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import reconcile_photos as reconcile

DATE_TAGS = ['DateTimeOriginal', 'SubSecDateTimeOriginal', 'CreationDate', 'CreateDate',
             'MediaCreateDate', 'TrackCreateDate', 'DateCreated', 'CreationTime']
PRIORITY = ['Composite:SubSecDateTimeOriginal', 'ExifIFD:DateTimeOriginal', 'XMP-exif:DateTimeOriginal',
            'Keys:CreationDate', 'UserData:DateTimeOriginal', 'PNG:CreationTime',
            'XMP-photoshop:DateCreated', 'XMP-xmp:CreateDate', 'ExifIFD:CreateDate',
            'QuickTime:CreateDate', 'Track1:MediaCreateDate', 'Track1:TrackCreateDate']


def calendar_path(path):
    return bool(re.fullmatch(r'media/\d{4}/(?:0[1-9]|1[0-2])/[^/]+', path))


def choose_date(metadata, item, tz):
    tags = PRIORITY + sorted(k for k in metadata if k not in PRIORITY and
                             k.split(':')[-1] in DATE_TAGS)
    for tag in tags:
        value = metadata.get(tag)
        if not isinstance(value, str):
            continue
        match = re.match(r'^(\d{4})[:-](\d{2})[:-](\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?', value)
        if not match:
            continue
        year, month, day = map(int, match.group(1, 2, 3))
        try:
            datetime(year, month, day)
        except ValueError:
            continue
        if not 1900 <= year <= datetime.now(timezone.utc).year + 1:
            continue
        if tag.startswith(('QuickTime:', 'Track')) and (year, month, day) == (1904, 1, 1):
            continue
        return dict(year=year, month=month, basis='embedded_metadata', tag=tag, value=value)
    ns = item['canonical_filesystem_mtime_ns']
    date = datetime.fromtimestamp(ns / 1e9, ZoneInfo(tz))
    return dict(year=date.year, month=date.month, basis='filesystem_mtime_fallback',
                tag='canonical_filesystem_mtime_ns', value=date.isoformat(),
                note='File modification date; may be export date, not capture date',
                metadata_read_error=metadata.get('Error'))


def extract_dates(items, root, exiftool, tz, cache_path):
    cached = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    missing = [item for item in items if item['sha256'] not in cached]
    tool = str(Path(exiftool).resolve()) if Path(exiftool).exists() else shutil.which(exiftool)
    if not tool:
        raise RuntimeError('ExifTool not found; supply --exiftool /path/to/exiftool')
    prefix = ['perl', tool] if Path(tool).name in {'exiftool', 'exiftool.pl'} else [tool]
    # ExifTool Perl script works without a system installation; reading only.
    if Path(tool).suffix.lower() == '.exe':
        prefix = [tool]
    env = dict(os.environ, TZ=tz)
    for offset in range(0, len(missing), 200):
        batch = missing[offset:offset+200]
        command = prefix + ['-j', '-G1', '-a', '-s', '-api', 'QuickTimeUTC=1']
        command += ['-'+tag for tag in DATE_TAGS]
        command += [str(root / item['target']) for item in batch]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError('ExifTool failed: ' + result.stderr[-1000:])
        try:
            rows = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError('ExifTool returned invalid metadata output')
        by_path = {str(Path(row['SourceFile']).resolve()): row for row in rows}
        for item in batch:
            row = by_path.get(str((root / item['target']).resolve()))
            if row is None:
                raise RuntimeError('No metadata record returned: ' + item['target'])
            cached[item['sha256']] = row
        reconcile.atomic_json(cache_path, cached)
        print('DATE_SCAN: {}/{} files'.format(len(items)-len(missing)+min(offset+200,len(missing)),len(items)), flush=True)
    return cached


def load_manifest(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def valid_relative(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != 'media':
        raise RuntimeError('Unsafe media path in plan')
    return path


def execute(root, state, exiftool, tz, apply):
    plan_path = state / 'move-plan.json'
    journal_path = state / 'journal.json'
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
    if journal and journal['root'] != str(root):
        raise RuntimeError('State directory belongs to a different archive')
    resuming = bool(journal and journal['state'] == 'moving')
    if resuming:
        manifest = load_manifest(state / 'manifest.before.jsonl')
        moves = json.loads(plan_path.read_text())
        summary = json.loads((state / 'summary.before.json').read_text())
        original_status = json.loads((state / 'STATUS.before.json').read_text())
        if not apply:
            print('Unfinished move journal found; add --apply to resume',flush=True)
            return 0
    else:
        original_status = json.loads((root / 'STATUS.json').read_text())
        if original_status.get('state') != 'complete':
            raise RuntimeError('Archive is not complete; finish its build before reorganization')
        manifest = load_manifest(root / 'manifest.jsonl')
        summary = json.loads((root / 'summary.json').read_text())
        pending = [item for item in manifest if item['target'].startswith('media/') and not calendar_path(item['target'])]
        if not pending:
            print('ALREADY_ORGANIZED: all media is in YYYY/MM folders', flush=True)
            return 0
        metadata = extract_dates(pending, root, exiftool, tz, state / 'date-cache.json')
        moves = []
        for item in pending:
            date = choose_date(metadata[item['sha256']], item, tz)
            target = (Path('media') / str(date['year']) / '{:02d}'.format(date['month']) / Path(item['target']).name).as_posix()
            moves.append(dict(sha256=item['sha256'], bytes=item['bytes'], old=item['target'],
                              new=target, date_organization=date))
        reconcile.atomic_json(plan_path, moves)
        print('PLAN: ' + json.dumps(dict(files=len(moves), basis=dict(Counter(m['date_organization']['basis'] for m in moves))), ensure_ascii=False), flush=True)
        if not apply:
            print('Plan only; use --apply to move files', flush=True)
            return 0
        # Keep recoverable control-file snapshots outside the upload archive.
        for name in ('manifest.jsonl', 'summary.json', 'STATUS.json'):
            shutil.copyfile(root / name, state / name.replace('.', '.before.', 1))
        reconcile.atomic_json(journal_path, dict(root=str(root), state='moving', files=len(moves)))
        reconcile.atomic_json(root / 'STATUS.json', dict(original_status, state='organizing_dates'))
    moved = resumed = 0
    started = last = time.monotonic()
    for n, move in enumerate(moves, 1):
        source = root / valid_relative(move['old'])
        target = root / valid_relative(move['new'])
        if target.exists():
            if source.exists() or target.is_symlink() or target.stat().st_size != move['bytes']:
                raise RuntimeError('Unexpected existing target; refusing overwrite: ' + str(target))
            resumed += 1
        else:
            if source.is_symlink() or not source.is_file() or source.stat().st_size != move['bytes']:
                raise RuntimeError('Missing or changed source: ' + str(source))
            before = reconcile.stamp(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
            if reconcile.stamp(target) != before:
                raise RuntimeError('File size/time changed during rename: ' + str(target))
            moved += 1
        now = time.monotonic()
        if now-last >= 10 or n == len(moves):
            print('MOVE: {}/{} moved={} resumed={}'.format(n,len(moves),moved,resumed), flush=True)
            last = now
    by_sha = {move['sha256']: move for move in moves}
    for item in manifest:
        if item['sha256'] in by_sha:
            move = by_sha[item['sha256']]
            item['target'] = move['new']
            item['date_organization'] = move['date_organization']
        elif item['target'].startswith('media/'):
            parts = Path(item['target']).parts
            item.setdefault('date_organization', dict(year=int(parts[1]),month=int(parts[2]),basis='existing_source_year_month'))
    hashes = set()
    expected = set()
    for item in manifest:
        if item['sha256'] in hashes:
            raise RuntimeError('Duplicate hash in manifest')
        hashes.add(item['sha256'])
        target = root / item['target']
        if not target.is_file() or target.stat().st_size != item['bytes']:
            raise RuntimeError('Output verification failed: ' + str(target))
        if item['target'].startswith('media/') and not calendar_path(item['target']):
            raise RuntimeError('Non-calendar media path remains')
        expected.add(item['target'])
    actual = set()
    for folder in ('media','sidecars'):
        for parent, dirs, files in os.walk(root / folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in files:
                if not name.startswith('.'):
                    actual.add((Path(parent)/name).relative_to(root).as_posix())
    if actual != expected:
        raise RuntimeError('Unexpected or missing visible files after move')
    # Only remove generated obsolete folder metadata, never media or original exports.
    old_folders = {str(Path(move['old']).parent) for move in moves}
    old_names = {(str(Path(move['old']).parent), Path(move['old']).name) for move in moves}
    metadata_removed = 0
    for folder in old_folders:
        path = root / folder
        if not path.exists():
            continue
        for child in path.iterdir():
            if child.name.startswith('._') and (folder,child.name[2:]) in old_names:
                with child.open('rb') as stream:
                    magic = stream.read(4)
                if magic == bytes.fromhex('00051607'):
                    child.unlink()
                    metadata_removed += 1
        if not any(path.iterdir()):
            path.rmdir()
    temp = root / 'manifest.jsonl.writing'
    with temp.open('w',encoding='utf-8') as stream:
        for item in manifest:
            stream.write(json.dumps(item,ensure_ascii=False)+'\n')
    os.replace(temp,root/'manifest.jsonl')
    info = dict(reorganized_files=len(moves), basis=dict(Counter(m['date_organization']['basis'] for m in moves)),
                timezone_for_utc_and_fallback=tz, layout='media/YYYY/MM',
                media_files=sum(item['target'].startswith('media/') for item in manifest),
                completed_at=datetime.now(timezone.utc).isoformat(),
                note='Filesystem fallback dates may be export dates; see calendar-moves.jsonl')
    summary['calendar_organization'] = info
    reconcile.atomic_json(root/'summary.json',summary)
    temp = root/'calendar-moves.jsonl.writing'
    with temp.open('w',encoding='utf-8') as stream:
        for move in moves:
            stream.write(json.dumps(move,ensure_ascii=False)+'\n')
    os.replace(temp,root/'calendar-moves.jsonl')
    reconcile.write_readme(root)
    reconcile.write_report(root,summary)
    reconcile.atomic_json(root/'STATUS.json',dict(original_status,state='complete',layout='calendar',
                         reorganized_at=info['completed_at']))
    reconcile.atomic_json(journal_path,dict(root=str(root),state='complete',files=len(moves),
                         generated_old_metadata_removed=metadata_removed))
    print('COMPLETE: '+json.dumps(info,ensure_ascii=False),flush=True)
    return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,default=Path('/Volumes/T7/photo-backup-iphone-icloud-2026-10-24'))
    parser.add_argument('--exiftool',default='exiftool')
    parser.add_argument('--timezone',default='Australia/Melbourne')
    parser.add_argument('--state-directory',type=Path,default=Path(__file__).resolve().parent/'.local-state'/'calendar')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    root=args.archive.expanduser().resolve()
    state=args.state_directory.expanduser().resolve()
    if root==state or root in state.parents:
        raise RuntimeError('State directory must be outside archive')
    if not (root/'.reconcile-owned.json').is_file():
        raise RuntimeError('Not a managed reconciliation archive')
    state.mkdir(parents=True,exist_ok=True)
    import fcntl
    with (state/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return execute(root,state,args.exiftool,args.timezone,args.apply)


if __name__=='__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('Interrupted; rerun with same archive/state to resume.',file=sys.stderr)
        sys.exit(130)
    except (OSError,RuntimeError,ValueError) as error:
        print('FAILED: '+str(error),file=sys.stderr)
        sys.exit(1)
