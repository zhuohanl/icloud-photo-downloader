#!/usr/bin/env python3
"""Locally pause/resume only this uploader during Melbourne working hours."""
import argparse
from datetime import datetime, time as wall_time, timedelta
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

DEFAULT_SCHEDULE = {'timezone': 'Australia/Melbourne', 'weekday_start': '20:00',
                    'weekday_end': '07:00', 'weekends': 'all_day'}


def validate_schedule(schedule):
    ZoneInfo(schedule['timezone'])
    start = wall_time.fromisoformat(schedule['weekday_start'])
    end = wall_time.fromisoformat(schedule['weekday_end'])
    if start <= end or schedule['weekends'] != 'all_day':
        raise ValueError('Expected an overnight weekday window and all-day weekends')


def allowed(now, schedule):
    local = now.astimezone(ZoneInfo(schedule['timezone']))
    if local.weekday() >= 5:
        return True
    clock = local.time().replace(tzinfo=None)
    return clock >= wall_time.fromisoformat(schedule['weekday_start']) or clock < wall_time.fromisoformat(schedule['weekday_end'])


def next_transition(now, schedule):
    zone = ZoneInfo(schedule['timezone'])
    local = now.astimezone(zone)
    candidates = []
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        for value in (schedule['weekday_start'], schedule['weekday_end']):
            boundary = datetime.combine(day, wall_time.fromisoformat(value), zone)
            if boundary.timestamp() > now.timestamp() and allowed(boundary - timedelta(seconds=1), schedule) != allowed(boundary, schedule):
                candidates.append(boundary)
    return min(candidates, key=lambda value: value.timestamp())


def identity(pid, uploader, config):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=,command='],
                            capture_output=True, text=True, check=False)
    value = result.stdout.strip()
    if result.returncode or str(uploader) not in value or str(config) not in value:
        return None
    return value


def atomic_json(path, value):
    temp = path.with_suffix('.writing')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(temp, path)


def supervise(args):
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text())
    schedule = config.get('upload_schedule', DEFAULT_SCHEDULE)
    validate_schedule(schedule)
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    uploader = Path(__file__).resolve().with_name('upload_archive.py')
    pid = args.pid
    token = identity(pid, uploader, config_path) if pid else None
    if pid and token is None:
        raise RuntimeError('PID does not belong to the expected uploader/config; no signal sent')
    child = None
    awake = None
    last_state = None
    with (args.state / 'schedule.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            while True:
                schedule = json.loads(config_path.read_text()).get('upload_schedule', DEFAULT_SCHEDULE)
                validate_schedule(schedule)
                now = datetime.now(ZoneInfo(schedule['timezone']))
                permit = allowed(now, schedule)
                if pid and (identity(pid, uploader, config_path) != token or (child is not None and child.poll() is not None)):
                    progress_path = args.state / 'progress.json'
                    progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
                    state = 'complete' if progress.get('state') == 'complete' else 'uploader_exited'
                    atomic_json(args.state / 'schedule-status.json', {
                        'state': state, 'pid': pid, 'checked_at': now.isoformat(), 'schedule': schedule})
                    print(state, flush=True)
                    return 0 if state == 'complete' else 1
                if permit and pid is None:
                    command = [sys.executable, '-u', str(uploader), '--config', str(config_path),
                               '--state', str(args.state.resolve()), '--execute', '--retry-transient']
                    with (args.state / 'upload.log').open('ab', buffering=0) as log:
                        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                                 stderr=subprocess.STDOUT, start_new_session=True)
                    pid = child.pid
                    token = identity(pid, uploader, config_path)
                    if token is None:
                        raise RuntimeError('Unable to confirm newly started uploader identity')
                    atomic_json(args.state / 'background.json', {'pid': pid, 'command': command,
                                'log': str(args.state.resolve() / 'upload.log')})
                state = 'uploading' if permit else 'paused_schedule'
                if state != last_state:
                    if pid:
                        # Stop all uploader threads immediately, including a multipart upload.
                        # Do not signal the process group: other jobs are never targeted.
                        os.kill(pid, signal.SIGCONT if permit else signal.SIGSTOP)
                    if awake is not None:
                        awake.terminate()
                        awake.wait(timeout=10)
                        awake = None
                    if permit and pid:
                        awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(pid)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print(f'{now.isoformat()} {state}; uploader_pid={pid}', flush=True)
                    last_state = state
                transition = next_transition(now, schedule)
                atomic_json(args.state / 'schedule-status.json', {
                    'state': state, 'pid': pid, 'checked_at': now.isoformat(),
                    'next_transition': transition.isoformat(), 'schedule': schedule})
                time.sleep(max(0.05, min(5, transition.timestamp() - now.timestamp())))
        finally:
            if awake is not None:
                awake.terminate()
                awake.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--pid', type=int, help='Attach only to an uploader with this exact config; otherwise start one during the next allowed window')
    parser.add_argument('--state', type=Path, default=Path(__file__).resolve().parents[1] / '.local-state/aws')
    args = parser.parse_args()
    try:
        return supervise(args)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
