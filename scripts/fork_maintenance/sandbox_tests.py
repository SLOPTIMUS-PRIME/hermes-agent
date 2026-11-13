#!/usr/bin/env python3
"""Run candidate Hermes checks in a credential-free, networkless Bubblewrap sandbox.

Used as the maintainer's test_command (candidate path is argv).  The candidate
is mounted read-only; the only writable mount is a fresh scratch directory.  The
sandbox gets a fresh private /proc (--unshare-all --proc), no capabilities, no
network, no host home and an empty environment.  The runtime (interpreter + the
pinned third-party packages) comes from a JSON file written by
prepare_runtime.py before any candidate code exists.

Hosted Ubuntu 24.04 blocks unprivileged user namespaces through AppArmor; the
workflow lifts that restriction on the ephemeral runner before this adapter
runs.  The adapter itself never needs elevated privileges.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

DEFAULT_TESTS = [
    'tests/agent/test_curator_conservative_prompt.py',
    'tests/tui_gateway/test_queued_prompt_stop_recovery.py',
    'tests/tui_gateway/test_queued_prompt_persistence.py',
    'tests/agent/test_curator.py',
    'tests/hermes_cli/test_model_allowlist.py',
]
BWRAP_CANDIDATES = ('/usr/bin/bwrap', '/usr/sbin/bwrap', '/usr/local/bin/bwrap')
HOST_ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8'}
PROBE_TIMEOUT = 60
SYNTAX_TIMEOUT = 60
PYTEST_TIMEOUT = 780
PYTEST_PER_TEST_TIMEOUT = 120

PREFIX = "import sys; sys.path[:0]=['/candidate','/deps']; "

SANDBOX_PROBE = r"""
import os, pathlib, socket
env_names = list(os.environ)
assert env_names and not any(
    k.upper().startswith(('GITHUB', 'CLAUDE', 'ANTHROPIC', 'ACTIONS_', 'RUNNER_', 'GIT_')) or
    k.upper().endswith(('_TOKEN', '_API_KEY', '_SECRET', '_PASSWORD')) for k in env_names), 'credential-like environment'
assert os.environ.get('HOME') == '/work/home'
for rel in ('/home/runner/.ssh/id_rsa', '/home/runner/.git-credentials', '/home/runner/.docker/config.json',
            '/home/runner/.config/gh/hosts.yml', '/home/nick/.hermes/config.yaml', '/home/nick/.hermes/.env',
            '/home/nick/.hermes/auth.json', '/home/nick/.config/gh/hosts.yml', '/home/nick/.ssh/id_ed25519',
            '/root/.ssh', '/github', '/run/secrets', '/var/run/docker.sock'):
    assert not pathlib.Path(rel).exists(), 'host path visible: ' + rel
caps = [l for l in pathlib.Path('/proc/self/status').read_text().splitlines() if l.startswith(('CapEff', 'CapPrm', 'CapBnd'))]
assert len(caps) == 3 and all(int(l.split()[1], 16) == 0 for l in caps), 'capabilities remain: ' + repr(caps)
pids = [n for n in os.listdir('/proc') if n.isdigit()]
assert len(pids) <= 8 and '1' in pids, 'host /proc visible: %d processes' % len(pids)
try:
    pathlib.Path('/candidate/.sandbox-write-probe').write_text('must fail')
except OSError: pass
else: raise AssertionError('candidate source is writable')
try:
    pathlib.Path('/usr/.sandbox-write-probe').write_text('must fail')
except OSError: pass
else: raise AssertionError('/usr is writable')
pathlib.Path('/work/tmp/ok').write_text('scratch is writable')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError: pass
else: raise AssertionError('sandbox has Internet access')
print('Sandbox: private /proc, no capabilities, host paths and credentials hidden, source read-only, network unavailable')
"""

SYNTAX_CHECK = """
import ast, pathlib
for rel in ['agent/curator.py','tui_gateway/session_auto_continue.py','hermes_cli/main.py','run_agent.py']:
    p=pathlib.Path('/candidate')/rel
    ast.parse(p.read_text(),filename=str(p))
print('Candidate syntax checks passed')
"""


def find_bwrap() -> str:
    for path in BWRAP_CANDIDATES:
        if os.access(path, os.X_OK):
            return path
    raise SystemExit('bubblewrap is not installed')


@functools.lru_cache(maxsize=None)
def supports_disable_userns(bwrap: str) -> bool:
    """Best-effort hardening: forbid nested user namespaces when this bwrap can do it.

    The capability drop is the primary control; this only shrinks kernel attack
    surface, so a bwrap that lacks or fails the option falls back without it.
    """
    trial = [bwrap, '--unshare-all', '--unshare-user', '--disable-userns', '--ro-bind', '/usr', '/usr',
             '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc',
             '/usr/bin/true']
    try:
        return subprocess.run(trial, capture_output=True, timeout=20, env=HOST_ENV).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def command(candidate: Path, scratch: Path, info: dict, payload: str, bwrap: str = '/usr/bin/bwrap',
            disable_userns: bool = False) -> list[str]:
    # No broad / or /home mount. The only host-writable mount is this run's scratch directory.
    cmd = [bwrap, '--die-with-parent', '--new-session', '--unshare-all', '--unshare-user']
    if disable_userns:
        cmd.append('--disable-userns')
    cmd += ['--cap-drop', 'ALL', '--clearenv', '--ro-bind', '/usr', '/usr',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--symlink', 'usr/bin', '/bin', '--proc', '/proc', '--dev', '/dev',
            '--dir', '/etc', '--ro-bind', '/etc/passwd', '/etc/passwd',
            '--ro-bind', '/etc/group', '/etc/group',
            '--ro-bind', info['prefix'], info['prefix'],
            '--ro-bind', info['site'], '/deps',
            '--ro-bind', str(candidate), '/candidate',
            '--bind', str(scratch), '/work',
            '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'HOME', '/work/home',
            '--setenv', 'HERMES_HOME', '/work/home/.hermes',
            '--setenv', 'TMPDIR', '/work/tmp',
            '--setenv', 'HERMES_DISABLE_LAZY_INSTALLS', '1',
            '--setenv', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1',
            '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
            '--setenv', 'TZ', 'UTC', '--setenv', 'LANG', 'C.UTF-8',
            '--chdir', '/candidate', info['python'], '-I', '-S', '-B', '-c', payload]
    return cmd


def pytest_payload() -> str:
    args = ['-q', '-p', 'pytest_asyncio.plugin', '-p', 'pytest_timeout', '-p', 'pytest_mock',
            '--timeout=%d' % PYTEST_PER_TEST_TIMEOUT, '--basetemp=/work/tmp/pytest',
            '-o', 'cache_dir=/work/pytest-cache', '--junitxml=/work/junit.xml', *DEFAULT_TESTS]
    return PREFIX + 'import pytest; raise SystemExit(pytest.main(' + repr(args) + '))'


def load_runtime(path: Path) -> dict:
    info = json.loads(path.read_text())
    for key in ('python', 'prefix', 'site'):
        if not isinstance(info.get(key), str) or not os.path.isabs(info[key]):
            raise SystemExit('Invalid test runtime description: ' + key)
        if not Path(info[key]).exists():
            raise SystemExit('Test runtime no longer exists: ' + key)
    return info


def sandbox(candidate: Path, scratch: Path, info: dict, payload: str, timeout: int):
    bwrap = find_bwrap()
    lockdown = supports_disable_userns(bwrap)
    if not lockdown:
        print('Sandbox note: nested user-namespace lockdown unavailable; capabilities remain dropped', file=sys.stderr)
    cmd = command(candidate, scratch, info, payload, bwrap, lockdown)
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=HOST_ENV)


def run_probe(candidate: Path, scratch_root: str | None, info: dict) -> int:
    with tempfile.TemporaryDirectory(prefix='fork-tests-', dir=scratch_root) as tmp:
        scratch = Path(tmp)
        (scratch / 'home').mkdir()
        (scratch / 'tmp').mkdir()
        r = sandbox(candidate, scratch, info, PREFIX + SANDBOX_PROBE, PROBE_TIMEOUT)
        print(r.stdout, end='')
        print(r.stderr, end='', file=sys.stderr)
        return r.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('candidate', type=Path, nargs='?')
    ap.add_argument('--runtime', type=Path, required=True)
    ap.add_argument('--scratch-root', default=None)
    ap.add_argument('--probe', action='store_true', help='only verify sandbox isolation on the candidate')
    ap.add_argument('--self-test', action='store_true', help='verify isolation on a throwaway candidate')
    args = ap.parse_args(argv)
    info = load_runtime(args.runtime)
    if args.self_test:
        with tempfile.TemporaryDirectory(prefix='fork-selftest-', dir=args.scratch_root) as tmp:
            return run_probe(Path(tmp).resolve(), args.scratch_root, info)
    if args.candidate is None:
        raise SystemExit('candidate path required')
    candidate = args.candidate.resolve(strict=True)
    rc = run_probe(candidate, args.scratch_root, info)
    if rc or args.probe:
        return rc
    for rel in DEFAULT_TESTS:
        p = candidate / rel
        if p.is_symlink() or not p.is_file():
            raise SystemExit('Missing required regression test: ' + rel)
    with tempfile.TemporaryDirectory(prefix='fork-tests-', dir=args.scratch_root) as tmp:
        scratch = Path(tmp)
        (scratch / 'home').mkdir()
        (scratch / 'tmp').mkdir()
        # Syntax check all changed production Python plus normal entrypoint modules, without importing them.
        r = sandbox(candidate, scratch, info, PREFIX + SYNTAX_CHECK, SYNTAX_TIMEOUT)
        print(r.stdout, end='')
        print(r.stderr, end='', file=sys.stderr)
        if r.returncode:
            return r.returncode
        r = sandbox(candidate, scratch, info, pytest_payload(), PYTEST_TIMEOUT + 30)
        print(r.stdout[-48000:], end='')
        print(r.stderr[-12000:], end='', file=sys.stderr)
        if r.returncode:
            return r.returncode
        doc = ET.parse(scratch / 'junit.xml').getroot()
        cases = list(doc.iter('testcase'))
        if len(cases) < 7 or any(list(c.iter('skipped')) for c in cases):
            raise SystemExit('Required regression scope was not fully exercised')
        print(json.dumps({'status': 'passed', 'tests': len(cases), 'isolation': 'bubblewrap-networkless-readonly-source'}))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        raise SystemExit('Sandbox test deadline exceeded')
