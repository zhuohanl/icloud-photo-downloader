#!/usr/bin/env python3
"""Content-exact, non-destructive union of photo exports. Python 3.9+, stdlib only."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from shlex import join as shlex_join
import sqlite3
import sys
import time
import unicodedata

VERSION = 1
CHUNK = 4 * 1024 * 1024
SIDECARS = {'.aae', '.xmp'}
IMAGES = {'.heic', '.heif', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.dng', '.tif', '.tiff'}
VIDEOS = {'.mov', '.mp4', '.m4v', '.3gp', '.avi', '.mkv'}
DEFAULT_SOURCES = [
    ('icloud', Path('/Volumes/T7/icloud-photos-backup/automatic')),
    ('iphone', Path('/Volumes/T7/iphone-photos-backup')),
    ('icloud-browser', Path('/Volumes/T7/icloud-photos-backup/2026-10-24')),
]


def stamp(path):
    value = path.stat()
    return (value.st_size, value.st_mtime_ns)


def digest(path, expected=None):
    before = stamp(path)
    if expected is not None and tuple(expected) != before:
        raise RuntimeError('File changed before reading: ' + str(path))
    hashed = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            hashed.update(block)
    if stamp(path) != before:
        raise RuntimeError('File changed while reading: ' + str(path))
    return hashed.hexdigest()


def inside(path, root):
    return path == root or root in path.parents


def inventory(sources):
    records = []
    counts = {}
    for rank, (label, root) in enumerate(sources):
        if not root.is_dir():
            raise RuntimeError('Source directory unavailable: ' + str(root))
        count = total = 0
        def fail(error):
            raise error
        for folder, dirs, files in os.walk(root, onerror=fail, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(folder) / d).is_symlink())
            for name in sorted(files):
                if name.startswith('.'):
                    continue
                path = Path(folder) / name
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError('Non-regular input file: ' + str(path))
                if path.suffix.lower() in {'.part', '.download', '.crdownload', '.tmp'}:
                    raise RuntimeError('Unfinished input file; finish download first: ' + str(path))
                size, mtime = stamp(path)
                if not size:
                    raise RuntimeError('Empty input file: ' + str(path))
                records.append(dict(source=label, root=str(root), relative=str(path.relative_to(root)),
                                    path=str(path), size=size, mtime=mtime, rank=rank))
                count += 1
                total += size
        counts[label] = dict(files=count, bytes=total)
    return records, counts


def open_cache(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, sha256 TEXT)')
    return connection


def hash_records(records, connection, workers):
    missing = []
    done = size_done = reused = 0
    total_bytes = sum(r['size'] for r in records)
    started = last = time.monotonic()
    for record in records:
        cached = connection.execute('SELECT size,mtime,sha256 FROM hashes WHERE path=?', (record['path'],)).fetchone()
        if cached and cached[:2] == (record['size'], record['mtime']):
            record['sha256'] = cached[2]
            done += 1
            reused += 1
            size_done += record['size']
        else:
            missing.append(record)
    print('HASH: {} files, {:.2f} GB; cached={} to-read={}'.format(
        len(records), total_bytes / 1e9, reused, len(missing)), flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(digest, Path(r['path']), (r['size'], r['mtime'])): r for r in missing}
        while futures:
            ready, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            for future in ready:
                record = futures.pop(future)
                record['sha256'] = future.result()
                connection.execute('INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)',
                                   (record['path'], record['size'], record['mtime'], record['sha256']))
                done += 1
                size_done += record['size']
            now = time.monotonic()
            if now - last >= 10 or not futures:
                connection.commit()
                print('HASH: {}/{} files, {:.2f}/{:.2f} GB, elapsed {:.0f}s'.format(
                    done, len(records), size_done / 1e9, total_bytes / 1e9, now - started), flush=True)
                last = now
    connection.commit()


def safe_stem(value):
    value = unicodedata.normalize('NFC', value)
    value = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', value).strip(' .')[:75] or 'file'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1, 10)), *('LPT'+str(i) for i in range(1, 10))}:
        value = '_' + value
    return value


def read_calendar_layout(destination):
    path = destination / 'manifest.jsonl'
    if not path.is_file():
        return {}
    layout = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        item = json.loads(line)
        if item.get('date_organization') and re.fullmatch(r'media/\d{4}/(?:0[1-9]|1[0-2])/[^/]+',item['target']):
            layout[item['sha256']] = dict(folder=str(Path(item['target']).parent), date_organization=item['date_organization'])
    return layout


def make_plan(records, calendar=None):
    calendar = calendar or {}
    grouped = defaultdict(list)
    for record in records:
        grouped[record['sha256']].append(record)
    plan = []
    for sha, members in grouped.items():
        date_organization = None
        canonical = min(members, key=lambda r: (r['rank'], r['relative']))
        path = Path(canonical['relative'])
        ext = path.suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', ext):
            ext = '.bin'
        if ext in SIDECARS:
            target = Path('sidecars') / sha[:2] / (sha + ext)
        else:
            parts = path.parts
            if canonical['source'] == 'icloud' and len(parts) >= 3 and re.fullmatch(r'\d{4}', parts[0]) and re.fullmatch(r'(0[1-9]|1[0-2])', parts[1]):
                folder = Path('media') / parts[0] / parts[1]
                date_organization = dict(year=int(parts[0]),month=int(parts[1]),basis='existing_source_year_month')
            else:
                folder = Path('media') / ('iphone-only' if all(r['source'] == 'iphone' for r in members) else 'unknown-date')
            if sha in calendar:
                folder = Path(calendar[sha]['folder'])
                date_organization = calendar[sha]['date_organization']
            target = folder / (safe_stem(path.stem) + '__' + sha + ext)
        planned = dict(sha256=sha, bytes=canonical['size'], target=target.as_posix(),
                         kind='sidecar' if ext in SIDECARS else 'image' if ext in IMAGES else 'video' if ext in VIDEOS else 'other',
                         canonical=canonical, members=members)
        if date_organization:
            planned['date_organization'] = date_organization
        plan.append(planned)
    return sorted(plan, key=lambda p: p['target'])


def associations(records):
    exact = {}
    stems = defaultdict(list)
    for record in records:
        path = Path(record['relative'])
        if path.suffix.lower() in SIDECARS:
            continue
        exact[(record['source'], record['relative'].casefold())] = record['sha256']
        stems[(record['source'], str(path.parent).casefold(), path.stem.casefold())].append(record['sha256'])
    linked = {}
    for record in records:
        path = Path(record['relative'])
        if path.suffix.lower() == '.xmp':
            match = exact.get((record['source'], str(path.with_suffix('')).casefold()))
            values = [match] if match else []
        elif path.suffix.lower() == '.aae':
            values = stems.get((record['source'], str(path.parent).casefold(), path.stem.casefold()), [])
        else:
            continue
        linked[(record['source'], record['relative'])] = sorted(set(values))
    return linked


def atomic_json(path, value):
    temp = path.with_name(path.name + '.writing')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temp, path)


def verify_source(record):
    if stamp(Path(record['path'])) != (record['size'], record['mtime']):
        raise RuntimeError('Source changed after hashing: ' + record['path'])


def materialize(item, destination):
    record = item['canonical']
    source = Path(record['path'])
    target = destination / item['target']
    expected = item['sha256']
    if target.exists():
        if target.is_symlink() or stamp(target)[0] != item['bytes'] or digest(target) != expected:
            raise RuntimeError('Existing output does not match; refusing overwrite: ' + str(target))
        return 'reused'
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = destination / '_partial'
    temp_dir.mkdir(exist_ok=True)
    temp = temp_dir / (expected + '.part')
    if temp.is_symlink():
        raise RuntimeError('Unexpected symlink in output: ' + str(temp))
    verify_source(record)
    hashed = hashlib.sha256()
    with source.open('rb') as reader, temp.open('wb') as writer:
        for block in iter(lambda: reader.read(CHUNK), b''):
            writer.write(block)
            hashed.update(block)
        writer.flush()
        os.fsync(writer.fileno())
    verify_source(record)
    if hashed.hexdigest() != expected or stamp(temp)[0] != item['bytes']:
        raise RuntimeError('Copy input checksum mismatch: ' + str(source))
    # Independent read-back of the written file before publishing its final name.
    if digest(temp) != expected:
        raise RuntimeError('SSD read-back checksum mismatch: ' + str(temp))
    # Preserve modification time without copying macOS xattrs/AppleDouble files.
    os.utime(temp, ns=(record['mtime'], record['mtime']))
    os.replace(temp, target)
    return 'copied'


def write_manifests(plan, records, destination):
    links = associations(records)
    manifest = destination / 'manifest.jsonl'
    temp = destination / 'manifest.jsonl.writing'
    with temp.open('w', encoding='utf-8') as stream:
        for item in plan:
            value = {k: item[k] for k in ('sha256', 'bytes', 'target', 'kind')}
            value['canonical_filesystem_mtime_ns'] = item['canonical']['mtime']
            if item.get('date_organization'):
                value['date_organization'] = item['date_organization']
            value['sources'] = [dict(source=r['source'], relative_path=r['relative'],
                                     filesystem_mtime_ns=r['mtime'],
                                     related_media_sha256=links.get((r['source'], r['relative']), []))
                                for r in sorted(item['members'], key=lambda r: (r['rank'], r['relative']))]
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')
    os.replace(temp, manifest)


def write_readme(destination):
    text = '''# Reconciled photo archive / 精确去重归档

media/ 保存照片、视频及其他原始文档。每个不同 SHA-256 内容只保存一份。
保留不同编辑成品、压缩版本和元数据不同的文件；不是相似画面去重。
media/YYYY/MM 的日期依据记录在 manifest.jsonl 的 date_organization 字段中。
优先保留内嵌拍摄日期或 iCloud 来源年月；使用文件日期的条目明确标记 filesystem_mtime_fallback。
文件日期可能是导出日期，不能当成已确认拍摄日期。calendar-moves.jsonl 记录日期整理的移动及依据。
尚未进行日期整理的新文件可能暂存于 iphone-only/unknown-date；运行 organize_calendar.py 后统一归入年月。

sidecars/ 保存去重后的 AAE、XMP。它们是附加信息，不是图片。
为了让内容完全相同的 sidecar 也只保存一份，它们没有全部放在原图片旁边；
manifest.jsonl 的 sources 记录原始文件名，并用 related_media_sha256 记录按文件名推断的配对。
一般查看器不会自动应用移动后的 AAE/XMP，恢复关联需使用清单；保留原备份便于完整恢复。
Live Photo 的照片和短视频都保留，不能仅凭此目录保证所有查看器自动识别配对。

manifest.jsonl 每行对应一个唯一内容，含完整 SHA-256、目标路径、大小和所有来源。
summary.json 包含来源数量、去重结果、手机独有内容数量等。
STATUS.json 的 state 必须为 complete，才能认为此轮构建校验已完成。
_partial/ 是中断恢复临时目录；构建中不得上传。

可以将本目录的 media/、sidecars/、manifest.jsonl、summary.json、REPORT.md、README.txt 和 STATUS.json 上传 AWS。
无需上传原始两份导出或本机 SQLite 哈希缓存。此工具本身不会上传，也不会修改原件。
精确去重与读取校验不等于证明 iPhone/iCloud 原始图库全部导出，也不等于完整解码测试。
文件数量与清单不包含 Mac 自动生成的隐藏 ._* 辅助文件。它们可能再次出现，上传 AWS 时排除隐藏文件。
'''
    command = ['aws', 's3', 'sync', str(destination), 's3://YOUR_BUCKET/photos/',
               '--exclude', '.*', '--exclude', '*/.*', '--exclude', '_partial/*',
               '--exclude', '*.writing', '--no-follow-symlinks', '--storage-class', 'INTELLIGENT_TIERING', '--dryrun']
    text += '\nAWS CLI 预演示例（需替换 YOUR_BUCKET；本工具没有执行此命令）：\n' + shlex_join(command) + '\n'
    text += '先核对预演清单，再决定是否去掉 --dryrun 实际上传。不要添加 --delete。\n'
    text += '参考：https://docs.aws.amazon.com/cli/latest/reference/s3/sync.html\n'
    if (destination / 'AWS-NAMING.md').is_file():
        text += '本归档的正式 bucket/prefix 规划见 AWS-NAMING.md；以上只是单目标 sync 语法预演，正式上传需区分媒体库和快照清单。\n'
    temp = destination / 'README.txt.writing'
    temp.write_text(text, encoding='utf-8')
    os.replace(temp, destination / 'README.txt')


def write_report(destination, summary):
    lines = ['# 照片合并与精确去重报告', '',
             '| 项目 | 结果 |', '|---|---:|',
             '| 输入文件 | {:,} |'.format(summary['input_files']),
             '| 输入总大小 | {:.2f} GB |'.format(summary['input_bytes']/1e9),
             '| 唯一内容文件 | {:,} |'.format(summary['unique_files']),
             '| 合并后大小 | {:.2f} GB |'.format(summary['unique_bytes']/1e9),
             '| 省去的重复副本 | {:,} |'.format(summary['duplicate_copies_removed']),
             '| 省去的重复内容大小 | {:.2f} GB |'.format(summary['bytes_saved']/1e9),
             '| 仅在手机导出中发现的唯一照片/视频内容 | {:,} |'.format(summary['iphone_only_media_files']),
             '', '## 输入来源', '', '| 来源 | 文件数 | GB |', '|---|---:|---:|']
    for label, counts in summary['source_counts'].items():
        lines.append('| {} | {:,} | {:.2f} |'.format(label, counts['files'], counts['bytes']/1e9))
    if summary.get('calendar_organization'):
        info=summary['calendar_organization']
        lines += ['', '## 年月目录整理', '',
                  '全部媒体已归入 media/YYYY/MM；本轮整理 {} 个文件。'.format(info.get('reorganized_files',0)),
                  '日期依据统计：`{}`。'.format(json.dumps(info.get('basis',{}),ensure_ascii=False)),
                  'filesystem_mtime_fallback 表示使用文件修改日期，可能是导出日期；明细见 calendar-moves.jsonl 和 manifest.jsonl。']
    if summary.get('sidecar_source_records_without_inferred_match'):
        lines += ['', '有 {} 条附加信息来源记录无法仅凭文件名确定配对。文件已保留，原路径见 manifest.jsonl；不代表对应照片一定缺失。'.format(summary['sidecar_source_records_without_inferred_match'])]
    lines += ['', '## 说明', '',
              '- 使用完整文件 SHA-256 去重；编辑版、压缩或元数据不同的文件均保留。',
              '- AAE/XMP 按内容保存在 sidecars/，原文件名及推断配对关系在 manifest.jsonl。',
              '- 手机独有表示未找到完全相同字节内容，不证明它一定没有上传 iCloud。',
              '- 每个新目标经过复制时校验和独立读回校验；原备份不变。',
              '- 这不等于完整图库导出证明，也不是所有媒体的解码测试。',
              '- 只在 STATUS.json 的 state 为 complete 时上传归档；本工具没有执行 AWS 上传。',
              '- 文件数不含说明文件和 Mac 隐藏辅助文件；上传时必须排除 ._* 等隐藏文件，预演命令见 README.txt。',
              '', '归档位置：`{}`'.format(summary['destination']), '']
    temp = destination / 'REPORT.md.writing'
    temp.write_text('\n'.join(lines), encoding='utf-8')
    os.replace(temp, destination / 'REPORT.md')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', metavar='LABEL=PATH')
    parser.add_argument('--destination', type=Path, default=Path('/Volumes/T7/photo-backup-iphone-icloud-2026-10-24'))
    parser.add_argument('--state-directory', type=Path, default=Path(__file__).resolve().parent / '.local-state' / 'reconcile')
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=2)
    parser.add_argument('--build', action='store_true', help='Copy and verify unique files; otherwise plan only')
    args = parser.parse_args(argv)
    sources = DEFAULT_SOURCES
    if args.source:
        sources = []
        for value in args.source:
            label, separator, path = value.partition('=')
            if not separator or not re.fullmatch(r'[a-zA-Z0-9_-]+', label):
                parser.error('--source must be LABEL=PATH with a simple unique label')
            sources.append((label, Path(path)))
    sources = [(label, root.expanduser().resolve()) for label, root in sources]
    if len({label for label, _ in sources}) != len(sources):
        parser.error('Source labels must be unique')
    destination = args.destination.expanduser().resolve()
    state = args.state_directory.expanduser().resolve()
    for i, (_, root) in enumerate(sources):
        if any((inside(destination, root), inside(root, destination), inside(state, root),
                inside(root, state), inside(state, destination), inside(destination, state))):
            raise RuntimeError('Sources, destination and state directory must not overlap')
        for _, other in sources[:i]:
            if inside(root, other) or inside(other, root):
                raise RuntimeError('Source directories must not overlap')
    if sys.platform == 'darwin' and len(destination.parts) >= 3 and destination.parts[1] == 'Volumes':
        if not Path(*destination.parts[:3]).is_mount():
            raise RuntimeError('Destination SSD is not mounted')
    state.mkdir(parents=True, exist_ok=True)
    # Advisory lock survives abrupt exits without stale-lock manual cleanup.
    import fcntl
    with (state / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another reconciliation is using this state directory')
        records, source_counts = inventory(sources)
        if not records:
            raise RuntimeError('No input files found')
        connection = open_cache(state / 'hashes.sqlite3')
        try:
            hash_records(records, connection, args.workers)
        finally:
            connection.close()
        existing_status = destination / 'STATUS.json'
        if existing_status.is_file() and json.loads(existing_status.read_text()).get('state') == 'organizing_dates':
            raise RuntimeError('Finish organize_calendar.py before rebuilding archive')
        plan = make_plan(records, read_calendar_layout(destination))
        unique_bytes = sum(p['bytes'] for p in plan)
        unique_types = Counter(p['kind'] for p in plan)
        phone_only = [p for p in plan if all(r['source'] == 'iphone' for r in p['members'])]
        summary = dict(version=VERSION, source_counts=source_counts, input_files=len(records),
                       input_bytes=sum(r['size'] for r in records), unique_files=len(plan),
                       unique_bytes=unique_bytes, unique_kinds=dict(unique_types),
                       duplicate_copies_removed=len(records)-len(plan),
                       bytes_saved=sum(r['size'] for r in records)-unique_bytes,
                       iphone_only_unique_files=len(phone_only),
                       iphone_only_media_files=sum(p['kind'] in {'image', 'video'} for p in phone_only),
                       iphone_only_bytes=sum(p['bytes'] for p in phone_only),
                       sidecar_source_records_without_inferred_match=sum(not v for v in associations(records).values()),
                       exact_dedup_only=True, source_content_cache='size + mtime_ns + absolute path',
                       destination=str(destination), created_at=datetime.now(timezone.utc).isoformat())
        atomic_json(state / 'plan-summary.json', summary)
        date_counts=Counter(p.get('date_organization',{}).get('basis','unorganized') for p in plan if p['kind']!='sidecar')
        if 'unorganized' not in date_counts:
            summary['calendar_organization']=dict(layout='media/YYYY/MM',basis=dict(date_counts),media_files=sum(date_counts.values()))
        print('PLAN: ' + json.dumps(summary, ensure_ascii=False), flush=True)
        if not args.build:
            print('Plan only; add --build to create archive. Sources unchanged.', flush=True)
            return 0
        identity = dict(version=VERSION, sources=[dict(label=l, root=str(r)) for l,r in sources])
        ownership = destination / '.reconcile-owned.json'
        if destination.exists():
            if not ownership.is_file() or json.loads(ownership.read_text()) != identity:
                raise RuntimeError('Destination not owned by this source configuration; refusing to reuse')
        else:
            destination.mkdir(parents=True)
            atomic_json(ownership, identity)
        atomic_json(destination / 'STATUS.json', dict(state='building', started_at=summary['created_at']))
        needed = sum(p['bytes'] for p in plan if not (destination / p['target']).exists())
        if shutil.disk_usage(destination).free < needed + max(2 * 1024**3, max(p['bytes'] for p in plan)):
            raise RuntimeError('Not enough free space for independent archive and temporary copy')
        copied = reused = complete_bytes = 0
        started = last = time.monotonic()
        for n, item in enumerate(plan, 1):
            result = materialize(item, destination)
            copied += result == 'copied'
            reused += result == 'reused'
            complete_bytes += item['bytes']
            now = time.monotonic()
            if now-last >= 10 or n == len(plan):
                print('COPY+VERIFY: {}/{} files, {:.2f}/{:.2f} GB, copied={} reused={}, elapsed {:.0f}s'.format(
                    n, len(plan), complete_bytes/1e9, unique_bytes/1e9, copied, reused, now-started), flush=True)
                last = now
        # Refuse a complete result if any input changed after the inventory snapshot.
        for record in records:
            verify_source(record)
        after, _ = inventory(sources)
        if {(r['path'],r['size'],r['mtime']) for r in after} != {(r['path'],r['size'],r['mtime']) for r in records}:
            raise RuntimeError('Source inventory changed during reconciliation; rerun')
        expected = {p['target'] for p in plan}
        actual = set()
        for folder in ('media', 'sidecars'):
            for base, _, files in os.walk(destination / folder):
                for name in files:
                    if not name.startswith('.'):
                        actual.add((Path(base) / name).relative_to(destination).as_posix())
        if actual != expected:
            raise RuntimeError('Output contains missing or unexpected non-hidden files; refusing complete status')
        partial = destination / '_partial'
        if partial.exists():
            # Only remove this job's empty temporary directory; never delete files.
            if any(partial.iterdir()):
                raise RuntimeError('Temporary files remain in output; refusing complete status')
            partial.rmdir()
        write_manifests(plan, records, destination)
        write_readme(destination)
        summary['copied_this_run'] = copied
        summary['reused_this_run'] = reused
        summary['verification'] = 'SHA-256 source hash + copy-time hash + destination read-back; reused outputs rehashed'
        atomic_json(destination / 'summary.json', summary)
        write_report(destination, summary)
        atomic_json(destination / 'STATUS.json', dict(state='complete', unique_files=len(plan),
                    unique_bytes=unique_bytes, manifest='manifest.jsonl',
                    completed_at=datetime.now(timezone.utc).isoformat()))
        print('COMPLETE: ' + str(destination), flush=True)
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nInterrupted. Sources unchanged; rerun same command to resume.', file=sys.stderr)
        sys.exit(130)
    except (OSError, RuntimeError, ValueError) as error:
        print('FAILED: ' + str(error), file=sys.stderr)
        sys.exit(1)
