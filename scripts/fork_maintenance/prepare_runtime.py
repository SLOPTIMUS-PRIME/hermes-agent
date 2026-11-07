#!/usr/bin/env python3
"""Describe a prepared virtualenv for sandbox_tests.py (runs before any candidate exists).

The sandbox runs the *base* interpreter with -I -S and an explicit sys.path, so
nothing from the virtualenv's own bin/ or .pth processing is trusted or needed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PROBE = ('import json,os,sys,sysconfig;'
         'print(json.dumps({"python":os.path.realpath(getattr(sys,"_base_executable",sys.executable)),'
         '"prefix":os.path.realpath(sys.base_prefix),"site":os.path.realpath(sysconfig.get_path("purelib"))}))')


def describe(venv: Path) -> dict:
    py = venv / 'bin' / 'python'
    r = subprocess.run([str(py), '-I', '-c', PROBE], text=True, capture_output=True, timeout=60, check=True)
    info = json.loads(r.stdout.strip().splitlines()[-1])
    prefix = info['prefix'].rstrip('/') + '/'
    if not info['python'].startswith(prefix) and not info['python'].startswith('/usr/'):
        raise SystemExit('Interpreter is not inside its prefix; refusing to describe runtime')
    if info['prefix'] in ('/', '/usr', '/home') or not Path(info['site']).is_dir():
        raise SystemExit('Unsafe or missing runtime location')
    # Required test tooling and the interpreter version are checked from the runtime itself.
    check = subprocess.run([str(py), '-I', '-c', 'import pytest, pytest_asyncio, pytest_timeout, pytest_mock, sys; print(sys.version_info[:2])'],
                           text=True, capture_output=True, timeout=60)
    if check.returncode:
        raise SystemExit('Test tooling missing from runtime: ' + check.stderr[-300:])
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--venv', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args(argv)
    info = describe(args.venv)
    args.out.write_text(json.dumps(info, indent=2) + '\n')
    print('Test runtime described: ' + info['python'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
