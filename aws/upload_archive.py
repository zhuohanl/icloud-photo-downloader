#!/usr/bin/env python3
"""Upload only manifest-owned payloads, verify S3 checksums, then publish a snapshot."""
import argparse
import base64
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import time

import boto3
from boto3.exceptions import S3UploadFailedError
from boto3.s3.transfer import S3Transfer, TransferConfig
from botocore.config import Config
from botocore.exceptions import (BotoCoreError, ClientError, ConnectionClosedError,
                                 ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)

PART_SIZE = 64 * 1024 * 1024
CONTROLS = ('manifest.jsonl', 'summary.json', 'calendar-moves.jsonl',
            'STATUS.json', 'REPORT.md', 'README.txt')


def atomic_json(path, value):
    temp = path.with_suffix('.writing')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    os.replace(temp, path)


def safe_path(root, target):
    parts = PurePosixPath(target).parts
    if (not parts or parts[0] not in ('media', 'sidecars') or
            any(p.startswith('.') for p in parts) or
            PurePosixPath(target).is_absolute() or '\\' in target):
        raise ValueError('Unsafe or unexpected manifest path')
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('Manifest path escapes archive or is a symbolic link')
    return path


def checksums(path, expected_sha=None):
    before = path.stat()
    whole = hashlib.sha256()
    parts = []
    with path.open('rb') as source:
        while block := source.read(PART_SIZE):
            whole.update(block)
            parts.append(hashlib.sha256(block).digest())
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('File changed while hashing: ' + str(path))
    digest = whole.hexdigest()
    if expected_sha is not None and digest != expected_sha:
        raise RuntimeError('Local SHA-256 differs from manifest: ' + str(path))
    if before.st_size >= PART_SIZE:
        checksum = base64.b64encode(hashlib.sha256(b''.join(parts)).digest()).decode()
        checksum += '-' + str(len(parts))
    else:
        checksum = base64.b64encode(whole.digest()).decode()
    return digest, checksum, (before.st_size, before.st_mtime_ns)


def load_archive(root):
    status = json.loads((root / 'STATUS.json').read_text())
    if status.get('state') != 'complete' or status.get('layout') != 'calendar':
        raise ValueError('Archive must be complete and organized by calendar')
    rows = [json.loads(line) for line in (root / 'manifest.jsonl').open()]
    targets, hashes = set(), set()
    for row in rows:
        if not re.fullmatch('[0-9a-f]{64}', row['sha256']):
            raise ValueError('Invalid manifest hash')
        if row['target'] in targets or row['sha256'] in hashes:
            raise ValueError('Manifest is not unique by path and SHA-256')
        targets.add(row['target'])
        hashes.add(row['sha256'])
        path = safe_path(root, row['target'])
        if path.stat().st_size != row['bytes']:
            raise ValueError('File size differs from manifest: ' + row['target'])
        if row['target'].startswith('media/') and not re.match(
                r'^media/\d{4}/(0[1-9]|1[0-2])/[^/]+$', row['target']):
            raise ValueError('Media is not organized by year/month')
    if (len(rows), sum(r['bytes'] for r in rows)) != (
            status['unique_files'], status['unique_bytes']):
        raise ValueError('Manifest totals do not match archive status')
    return rows


def select_sample(rows, limit):
    selected = rows[:limit]
    multipart = [r for r in rows if r['bytes'] >= PART_SIZE]
    if limit >= 2 and multipart and not any(r['bytes'] >= PART_SIZE for r in selected):
        selected = selected[:-1] + [min(multipart, key=lambda row: row['bytes'])]
    return selected


def validate_config(config):
    if config.get('region') != 'ap-southeast-2':
        raise ValueError('This workflow requires the approved region ap-southeast-2')
    if config.get('storage_class') != 'INTELLIGENT_TIERING':
        raise ValueError('This workflow requires INTELLIGENT_TIERING')
    if not re.fullmatch(r'personal-archive-ap-southeast-2-[a-z0-9-]{4,25}', config.get('bucket') or ''):
        raise ValueError('Set a bucket matching the approved naming pattern')
    if not re.fullmatch(r'\d{12}', config.get('expected_account') or ''):
        raise ValueError('Set expected_account to guard against the wrong AWS account')
    if config.get('library_prefix') != 'photos/library/v1/' or config.get('snapshots_prefix') != 'photos/snapshots/':
        raise ValueError('Unexpected library or snapshot prefix')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', config.get('snapshot_date') or ''):
        raise ValueError('Invalid snapshot date')


class ArchiveUploader:
    def __init__(self, client, transfer, config):
        self.client, self.transfer, self.config = client, transfer, config
        self.bucket = config['bucket']
        self.owner = config['expected_account']
        self.bytes_sent = 0
        self.lock = threading.Lock()

    def callback(self, count):
        with self.lock:
            self.bytes_sent += count

    def inventory(self, prefix):
        return {item['Key']: item for page in self.client.get_paginator('list_objects_v2').paginate(
            Bucket=self.bucket, Prefix=prefix, ExpectedBucketOwner=self.owner)
                for item in page.get('Contents', [])}

    def verify(self, key, size, sha, checksum):
        head = self.client.head_object(Bucket=self.bucket, Key=key,
                                       ExpectedBucketOwner=self.owner, ChecksumMode='ENABLED')
        if (head['ContentLength'] != size or
                head.get('ChecksumSHA256') != checksum or
                head.get('Metadata', {}).get('sha256') != sha or
                head.get('StorageClass') != self.config['storage_class'] or
                head.get('ServerSideEncryption') != 'AES256'):
            raise RuntimeError('S3 object does not match expected content/configuration: ' + key)
        return head

    def upload_one(self, path, key, expected_sha, exists):
        sha, checksum, stat = checksums(path, expected_sha)
        if not exists:
            self.transfer.upload_file(str(path), self.bucket, key, extra_args={
                'ExpectedBucketOwner': self.owner,
                'StorageClass': self.config['storage_class'],
                'ServerSideEncryption': 'AES256', 'ChecksumAlgorithm': 'SHA256',
                'Metadata': {'sha256': sha, 'layout-version': '1'},
            }, callback=self.callback)
        # An existing mismatched object is never overwritten automatically.
        self.verify(key, stat[0], sha, checksum)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != stat:
            raise RuntimeError('Local file changed during upload: ' + str(path))
        return {'key': key, 'bytes': stat[0], 'sha256': sha,
                's3_checksum_sha256': checksum, 'reused': exists}


def ensure_bucket(s3, config, create):
    bucket, owner = config['bucket'], config['expected_account']
    if create:
        # Use only for a fresh, persisted random name; no existing bucket is reconfigured.
        s3.create_bucket(Bucket=bucket,
                         CreateBucketConfiguration={'LocationConstraint': config['region']},
                         ObjectOwnership='BucketOwnerEnforced')
        s3.put_public_access_block(Bucket=bucket, ExpectedBucketOwner=owner,
            PublicAccessBlockConfiguration={
                'BlockPublicAcls': True, 'IgnorePublicAcls': True,
                'BlockPublicPolicy': True, 'RestrictPublicBuckets': True})
        s3.put_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=owner,
            ServerSideEncryptionConfiguration={'Rules': [
                {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]})
    s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=owner)
    location = s3.get_bucket_location(Bucket=bucket, ExpectedBucketOwner=owner)['LocationConstraint']
    block = s3.get_public_access_block(Bucket=bucket, ExpectedBucketOwner=owner)['PublicAccessBlockConfiguration']
    ownership = s3.get_bucket_ownership_controls(Bucket=bucket, ExpectedBucketOwner=owner)['OwnershipControls']
    encryption = s3.get_bucket_encryption(Bucket=bucket, ExpectedBucketOwner=owner)['ServerSideEncryptionConfiguration']
    if (location != config['region'] or not all(block.get(k) for k in (
            'BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')) or
            ownership['Rules'][0]['ObjectOwnership'] != 'BucketOwnerEnforced' or
            encryption['Rules'][0]['ApplyServerSideEncryptionByDefault']['SSEAlgorithm'] != 'AES256'):
        raise RuntimeError('Bucket region or security settings differ from the approved plan')


def run(args):
    config = json.loads(args.config.read_text())
    validate_config(config)
    rows = load_archive(args.archive)
    total_bytes = sum(r['bytes'] for r in rows)
    print(f'Plan: {len(rows)} payloads, {total_bytes / 1e9:.2f} GB; bucket={config["bucket"]}', flush=True)
    if not args.execute:
        return
    if args.workers < 1 or args.workers > 4 or args.bandwidth_mib < 1:
        raise ValueError('Use 1-4 workers and a positive bandwidth limit')
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (args.state / 'upload.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        upload_to_aws(args, config, rows, total_bytes)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def upload_to_aws(args, config, rows, total_bytes):
    client_config = Config(retries={'total_max_attempts': 10, 'mode': 'standard'},
                           connect_timeout=15, read_timeout=120, max_pool_connections=8)
    session = boto3.Session(region_name=config['region'])
    account = session.client('sts', config=client_config).get_caller_identity()['Account']
    if account != config['expected_account']:
        raise RuntimeError('AWS identity differs from expected_account')
    s3 = session.client('s3', config=client_config)
    ensure_bucket(s3, config, args.create_bucket)
    transfer_config = TransferConfig(
        multipart_threshold=PART_SIZE, multipart_chunksize=PART_SIZE,
        max_concurrency=args.workers, max_bandwidth=args.bandwidth_mib * 1024**2,
        preferred_transfer_client='classic')
    started = time.monotonic()
    manifest_sha = checksums(args.archive / 'manifest.jsonl')[0]
    snapshot_prefix = config['snapshots_prefix'] + config['snapshot_date'] + '/'
    progress = {'state': 'uploading', 'bucket': config['bucket'], 'region': config['region'],
                'storage_class': config['storage_class'], 'manifest_sha256': manifest_sha,
                'payload_total': len(rows), 'payload_bytes': total_bytes,
                'verified_files': 0, 'verified_bytes': 0, 'reused_files': 0}
    progress_path = args.state / 'progress.json'
    def save_progress():
        progress['updated_at'] = datetime.now(timezone.utc).isoformat()
        progress['elapsed_seconds'] = round(time.monotonic() - started)
        progress['bytes_sent_this_run'] = uploader.bytes_sent
        atomic_json(progress_path, progress)
        print(json.dumps(progress, ensure_ascii=False), flush=True)
    with S3Transfer(s3, transfer_config) as transfer:
        uploader = ArchiveUploader(s3, transfer, config)
        existing = uploader.inventory(config['library_prefix'])
        selected = select_sample(rows, args.limit) if args.limit else rows
        iterator = iter(selected)
        pending = set()
        def enqueue(pool):
            row = next(iterator, None)
            if row is None:
                return False
            key = config['library_prefix'] + row['target']
            pending.add(pool.submit(uploader.upload_one, safe_path(args.archive, row['target']),
                                    key, row['sha256'], key in existing))
            return True
        save_progress()
        try:
            with (args.state / 'verified.jsonl').open('a') as journal, ThreadPoolExecutor(max_workers=args.workers) as pool:
                for _ in range(args.workers * 2):
                    enqueue(pool)
                last_progress = time.monotonic()
                while pending:
                    done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        result = future.result()
                        journal.write(json.dumps(result) + '\n')
                        journal.flush()
                        progress['verified_files'] += 1
                        progress['verified_bytes'] += result['bytes']
                        progress['reused_files'] += int(result['reused'])
                        enqueue(pool)
                    if time.monotonic() - last_progress >= 30:
                        save_progress()
                        last_progress = time.monotonic()
            if args.limit:
                progress['state'] = 'sample_verified'
                save_progress()
                return
            progress['state'] = 'publishing_snapshot'
            save_progress()
            if checksums(args.archive / 'manifest.jsonl')[0] != manifest_sha:
                raise RuntimeError('Manifest changed during upload; snapshot publication stopped')
            snapshot_existing = uploader.inventory(snapshot_prefix)
            # Publish controls only after every payload has passed checksum verification.
            snapshot_results = []
            for name in CONTROLS:
                path = args.archive / name
                key = snapshot_prefix + name
                snapshot_results.append(uploader.upload_one(path, key, None, key in snapshot_existing))
            layout = {k: config[k] for k in ('region', 'bucket', 'storage_class',
                      'layout_version', 'library_prefix', 'snapshots_prefix', 'snapshot_date')}
            layout['manifest_sha256'] = manifest_sha
            layout['restore'] = 'Resolve each manifest target relative to library_prefix; verify full-file SHA-256.'
            layout_path = args.state / 'cloud-layout.json'
            atomic_json(layout_path, layout)
            layout_key = snapshot_prefix + layout_path.name
            snapshot_results.append(uploader.upload_one(layout_path, layout_key, None, layout_key in snapshot_existing))
            remote = uploader.inventory(config['library_prefix'])
            for row in rows:
                key = config['library_prefix'] + row['target']
                if key not in remote or remote[key]['Size'] != row['bytes']:
                    raise RuntimeError('Final inventory differs from manifest')
            if checksums(args.archive / 'manifest.jsonl')[0] != manifest_sha:
                raise RuntimeError('Manifest changed while publishing snapshot')
            marker = {'state': 'complete', 'manifest_sha256': manifest_sha,
                      'layout': layout, 'payload_files': len(rows), 'payload_bytes': total_bytes,
                      'snapshot_files': len(snapshot_results),
                      'verification': 'Local full-file SHA-256 matches manifest; every S3 SHA-256 full/composite checksum matches local computation; size, class, SSE-S3 checked.'}
            marker_path = args.state / 'UPLOAD-VERIFIED.json'
            atomic_json(marker_path, marker)
            marker_key = snapshot_prefix + marker_path.name
            uploader.upload_one(marker_path, marker_key, None, marker_key in snapshot_existing)
            progress['state'] = 'complete'
            progress['snapshot_prefix'] = snapshot_prefix
            progress['remote_extra_library_objects'] = len(remote) - len(rows)
            save_progress()
        except BaseException as error:
            progress['state'] = 'interrupted'
            progress['error_type'] = type(error).__name__
            save_progress()
            raise


def transient_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (ConnectionClosedError, ConnectTimeoutError,
                              EndpointConnectionError, ReadTimeoutError)):
            return True
        if isinstance(error, ClientError):
            status = error.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
            code = error.response.get('Error', {}).get('Code')
            if status >= 500 or code in ('SlowDown', 'RequestTimeout', 'Throttling'):
                return True
        error = error.__cause__ or error.__context__
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--archive', type=Path, default=Path('/Volumes/T7/photo-backup-iphone-icloud-2026-10-24'))
    parser.add_argument('--state', type=Path, default=Path(__file__).resolve().parents[1] / '.local-state/aws')
    parser.add_argument('--execute', action='store_true', help='Without this flag, validate local files and print plan only')
    parser.add_argument('--create-bucket', action='store_true', help='Create a NEW persisted bucket name on the first run only')
    parser.add_argument('--limit', type=int, help='Verify a small payload sample; do not publish snapshot or completion marker')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--bandwidth-mib', type=int, default=20, help='Shared transfer bandwidth cap in MiB/s')
    parser.add_argument('--retry-transient', action='store_true', help='Retry connection/service failures with backoff; integrity and permission errors stop')
    args = parser.parse_args()
    failures = 0
    while True:
        try:
            if args.limit is not None and args.limit < 1:
                raise ValueError('--limit must be positive')
            run(args)
            return 0
        except (BotoCoreError, ClientError, S3UploadFailedError, ValueError, RuntimeError, OSError) as error:
            print(f'{type(error).__name__}: {error}', file=sys.stderr, flush=True)
            if not args.retry_transient or not transient_error(error):
                return 1
            failures += 1
            delay = min(900, 30 * 2 ** min(failures - 1, 5))
            print(f'Transient failure; resume after {delay}s. Existing verified objects are retained.', flush=True)
            time.sleep(delay)


if __name__ == '__main__':
    sys.exit(main())
