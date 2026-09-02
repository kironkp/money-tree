"""Spawn management commands as detached subprocesses (experiments, replays,
history syncs, the agent itself) and read their logs back for the UI."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from django.conf import settings


def log_path(name: str) -> Path:
    settings.RUN_DIR.mkdir(exist_ok=True)
    return settings.RUN_DIR / f'{name}.log'


def spawn_manage(args: list[str], log_name: str, nice: int = 0) -> int:
    """Run `manage.py <args>` detached; returns the pid."""
    path = log_path(log_name)
    fh = open(path, 'ab')
    env = dict(os.environ)
    env.setdefault('DJANGO_SETTINGS_MODULE', 'moneytree.settings')
    env['PYTHONUNBUFFERED'] = '1'
    cmd = [sys.executable, str(settings.BASE_DIR / 'manage.py'), *args]
    proc = subprocess.Popen(cmd, cwd=settings.BASE_DIR, stdout=fh, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True,
                            preexec_fn=(lambda: os.nice(nice)) if nice else None)
    fh.close()
    return proc.pid


def tail(log_name: str, lines: int = 40) -> str:
    path = log_path(log_name)
    if not path.exists():
        return ''
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64_000))
            data = fh.read().decode('utf-8', 'replace')
    except OSError:
        return ''
    return '\n'.join(data.splitlines()[-lines:])


def alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(pid: int) -> bool:
    if not alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False
