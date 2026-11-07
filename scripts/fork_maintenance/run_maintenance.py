#!/usr/bin/env python3
"""GitHub Actions entrypoint for the scheduled Hermes fork maintenance.

Builds the maintainer config from policy.json and the runner's real paths, hands
the tested maintainer.py protocol exactly the credentials it needs and no more,
and guarantees that <work-dir>/state/latest.json exists when the run ends.

Credentials are read once from the step environment (CLAUDE_CODE_OAUTH_TOKEN,
GITHUB_TOKEN) and removed from it:
  * the Claude OAuth token goes to a 0600 file only the Claude adapter reads;
  * the GitHub token reaches the maintainer only as a git http.extraheader scoped
    to the fork URL (used for the compare-and-swap push and branch readback);
  * repair_probe mode never receives the GitHub token and never publishes.
The candidate test sandbox, merge machinery and Claude process never see both.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SHA_RE = re.compile(r'^[0-9a-f]{40}$')
MAINTAINER_DEADLINE = 3300
MAX_LATEST_BYTES = 900_000
PROBE_FILE = 'agent/curator.py'
PROBE_BASE = 'DESCRIPTION_LIMIT = 57\n\n\ndef describe():\n    return "limit %d" % DESCRIPTION_LIMIT\n'
PROBE_UPSTREAM = 'DESCRIPTION_LIMIT = 60\n\n\ndef describe():\n    return "limit %d" % DESCRIPTION_LIMIT\n'
PROBE_FORK = 'DESCRIPTION_LIMIT = 57\nCONSERVATIVE_CURATION = True\n\n\ndef describe():\n    return "limit %d" % DESCRIPTION_LIMIT\n'
PROBE_INTENT = ('Synthetic conflict probe. Upstream raised DESCRIPTION_LIMIT from 57 to 60: accept that. '
                'The fork patch adds CONSERVATIVE_CURATION = True: keep it. The resolved file must define '
                'DESCRIPTION_LIMIT = 60 and CONSERVATIVE_CURATION = True and keep describe() unchanged.')


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load_policy() -> dict:
    return json.loads((ROOT / 'policy.json').read_text())


def build_config(policy: dict, *, seed_repo: str, seed_sha: str, state_dir: str, runtime: str,
                 scratch_root: str, claude_bin: str, token_file: str, python: str | None = None) -> dict:
    """Portable maintainer config: the policy plus this runner's concrete adapter argv."""
    python = python or sys.executable
    cfg = dict(policy)
    cfg.update(
        seed_repo=seed_repo, seed_sha=seed_sha, state_dir=state_dir,
        repair_command=[python, str(ROOT / 'claude_repair.py'), '--claude-bin', claude_bin,
                        '--token-file', token_file],
        test_command=[python, str(ROOT / 'sandbox_tests.py'), '--runtime', runtime,
                      '--scratch-root', scratch_root, '{candidate}'])
    return cfg


def git_publish_env(token: str, fork_url: str) -> tuple[dict, str]:
    """git config via environment, scoped to the fork URL so no other remote sees the token."""
    header = base64.b64encode(b'x-access-token:' + token.encode()).decode()
    return ({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.' + fork_url + '.extraheader',
             'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + header}, header)


def maintainer_env(home: str, scratch_root: str, github_token: str | None, fork_url: str) -> tuple[dict, list[str]]:
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': home, 'TMPDIR': scratch_root,
           'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_TERMINAL_PROMPT': '0'}
    secrets: list[str] = []
    if github_token:
        extra, header = git_publish_env(github_token, fork_url)
        env.update(extra)
        secrets += [github_token, header]
    return env, secrets


def failure_doc(cfg: dict, reason: str, started: str, publish_requested: bool, exit_code: int = 1) -> dict:
    """Schema-1 latest.json for runs that failed before the maintainer could write its own."""
    return {
        'schema': 1, 'started_utc': started, 'finished_utc': utcnow(), 'status': 'failed',
        'reason': reason[:800], 'exit_code': exit_code,
        'input': {k: cfg[k] for k in ('official_url', 'official_branch', 'fork_url', 'deployment_branch',
                                      'seed_repo', 'seed_sha')} | {'publish_requested': publish_requested},
        'official': {'url': cfg['official_url'], 'branch': cfg['official_branch'], 'sha': None},
        'fork': {'url': cfg['fork_url'], 'branch': cfg['deployment_branch'],
                 'published_before': None, 'published_after': None},
        'seed': {'repo': cfg['seed_repo'], 'sha': cfg['seed_sha'], 'available': False, 'used_as_base': False},
        'base': None, 'candidate': None, 'merges': [],
        'lineage': {'seed_integrated': False, 'published_integrated': False},
        'repairs': {'max': cfg.get('max_repairs', 3), 'attempts': 0, 'results': [], 'last': None},
        'checks': {'runtime': None, 'tests': None},
        'publish': {'requested': publish_requested, 'status': 'not-attempted', 'expected_old': None,
                    'remote_after': None, 'method': None, 'reason': reason[:800]},
        'candidate_checkout': os.path.join(cfg['state_dir'], 'candidate'), 'events': [],
    }


def write_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + '.%d.tmp' % os.getpid())
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh, indent=2)
        fh.write('\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def finalize_latest(cfg: dict, started: str, publish_requested: bool, rc: int, secrets: list[str]) -> dict:
    """Make state/latest.json exist, be valid, bounded and free of credential material."""
    latest = Path(cfg['state_dir']) / 'latest.json'
    doc = None
    problem = ''
    try:
        raw = latest.read_text(encoding='utf-8')
        if any(s and s in raw for s in secrets):
            problem = 'result contained credential material and was withheld'
        else:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or doc.get('schema') != 1 or not isinstance(doc.get('status'), str):
                doc, problem = None, 'maintainer wrote an invalid result document'
    except FileNotFoundError:
        problem = 'maintainer exited (rc=%s) without writing a result' % rc
    except (OSError, ValueError) as exc:
        problem = 'maintainer result unreadable: ' + type(exc).__name__
    if doc is None:
        doc = failure_doc(cfg, problem, started, publish_requested, rc or 1)
    elif latest.stat().st_size > MAX_LATEST_BYTES:
        doc['events'] = []
        doc.setdefault('repairs', {})['results'] = []
    else:
        return doc  # the maintainer's own bytes are kept exactly
    write_atomic(latest, doc)
    return doc


def run_group(argv: list[str], env: dict, cwd: str, timeout: float) -> int:
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        print('Maintenance exceeded its overall deadline', file=sys.stderr)
        return 124


def git(*args: str, cwd: str, env: dict | None = None) -> str:
    base = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'HOME': cwd}
    base.update(env or {})
    r = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=cwd, env=base, text=True,
                       capture_output=True, timeout=120)
    if r.returncode:
        raise RuntimeError('git %s failed: %s' % (args[0], r.stderr.strip()[-300:]))
    return r.stdout.strip()


def seed_identity(seed: str, branch: str) -> str:
    if git('symbolic-ref', '--short', 'HEAD', cwd=seed) != branch:
        raise RuntimeError('seed checkout is not the %s branch' % branch)
    sha = git('rev-parse', 'HEAD', cwd=seed)
    if not SHA_RE.match(sha):
        raise RuntimeError('seed HEAD is not a full SHA')
    return sha


def build_probe_fixture(root: Path) -> dict:
    """Disposable real-git conflict: upstream and the fork patch edit the same lines."""
    env = {'GIT_AUTHOR_NAME': 'probe', 'GIT_AUTHOR_EMAIL': 'probe@example.invalid',
           'GIT_COMMITTER_NAME': 'probe', 'GIT_COMMITTER_EMAIL': 'probe@example.invalid',
           'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z'}
    work = root / 'work'
    work.mkdir()
    git('init', '-q', '-b', 'main', cwd=str(work))
    (work / 'agent').mkdir()
    (work / PROBE_FILE).write_text(PROBE_BASE)
    git('add', '.', cwd=str(work))
    git('commit', '-qm', 'base', cwd=str(work), env=env)
    git('checkout', '-q', '-b', 'nick-patches', cwd=str(work))
    (work / PROBE_FILE).write_text(PROBE_FORK)
    git('commit', '-qam', 'fork patch', cwd=str(work), env=env)
    git('checkout', '-q', 'main', cwd=str(work))
    (work / PROBE_FILE).write_text(PROBE_UPSTREAM)
    git('commit', '-qam', 'upstream change', cwd=str(work), env=env)
    official = root / 'official.git'
    fork = root / 'fork.git'
    for bare, branch in ((official, 'main'), (fork, 'nick-patches')):
        git('init', '-q', '--bare', str(bare), cwd=str(root))
        git('push', '-q', str(bare), 'refs/heads/%s:refs/heads/%s' % (branch, branch), cwd=str(work))
    git('checkout', '-q', 'nick-patches', cwd=str(work))
    return {'official': str(official), 'fork': str(fork), 'seed': str(work), 'seed_sha': git('rev-parse', 'HEAD', cwd=str(work))}


def probe_policy(fixture: dict) -> dict:
    return {'official_url': fixture['official'], 'official_branch': 'main', 'fork_url': fixture['fork'],
            'deployment_branch': 'nick-patches', 'allowed_edit_paths': [PROBE_FILE],
            'patch_intents': {PROBE_FILE: PROBE_INTENT}, 'max_repairs': 1, 'max_input_bytes': 750000,
            'max_edit_bytes': 300000, 'reuse_validation': False, 'command_timeout': 600, 'test_timeout': 120}


def evaluate_probe(doc: dict) -> str:
    """Return '' when the real Claude response resolved the conflict through the protocol."""
    merges = doc.get('merges') or []
    if doc.get('status') != 'prepared' or doc.get('exit_code') != 0:
        return 'probe run ended %s: %s' % (doc.get('status'), doc.get('reason'))
    if (doc.get('repairs') or {}).get('attempts') != 1 or not any(m.get('result') == 'merged+repaired' for m in merges):
        return 'probe did not exercise a conflict repair'
    if (doc.get('checks', {}).get('tests') or {}).get('status') != 'pass':
        return 'probe resolution did not pass its check'
    return ''


def sandbox_self_test(runtime: str, scratch_root: str) -> None:
    """Verify the isolation primitive before any candidate is built."""
    sc = subprocess.run([sys.executable, str(ROOT / 'sandbox_tests.py'), '--runtime', runtime,
                         '--scratch-root', scratch_root, '--self-test'], env={'PATH': '/usr/bin:/bin'},
                        text=True, capture_output=True, timeout=180)
    sys.stdout.write(sc.stdout)
    sys.stderr.write(sc.stderr)
    if sc.returncode:
        raise RuntimeError('sandbox self-test failed: ' + (sc.stderr or sc.stdout).strip()[-300:])


def ensure_result(work: Path, started: str) -> int:
    """Guarantee an uploadable latest.json when an earlier workflow step failed."""
    state = work / 'state'
    latest = state / 'latest.json'
    if latest.is_file():
        return 0
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = build_config(load_policy(), seed_repo='', seed_sha='', state_dir=str(state), runtime='',
                       scratch_root='', claude_bin='', token_file='')
    write_atomic(latest, failure_doc(cfg, 'workflow failed before the maintenance runner produced a result',
                                     started, True))
    print('wrote failure result for a run that never reached the maintainer', file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--work-dir', type=Path, required=True)
    ap.add_argument('--seed-repo', type=Path, default=None, help='required except for --repair-probe')
    ap.add_argument('--runtime', type=Path, default=None, help='required except for --repair-probe')
    ap.add_argument('--claude-bin', default='')
    ap.add_argument('--repair-probe', action='store_true')
    ap.add_argument('--ensure-result', action='store_true',
                    help='only write a failure latest.json if none exists (runs even after step failures)')
    args = ap.parse_args(argv)
    if args.ensure_result:
        return ensure_result(args.work_dir.resolve(), utcnow())
    if not args.claude_bin:
        ap.error('--claude-bin is required')
    if (args.runtime is None or args.seed_repo is None) and not args.repair_probe:
        ap.error('--runtime and --seed-repo are required unless --repair-probe is used')
    runtime = str(args.runtime) if args.runtime else ''

    claude_token = os.environ.pop('CLAUDE_CODE_OAUTH_TOKEN', '')
    github_token = os.environ.pop('GITHUB_TOKEN', '')
    started = utcnow()
    policy = load_policy()
    work = args.work_dir.resolve()
    state_dir = work / 'state'
    marker = work / '.fork-maintenance-work'
    if work.exists() and any(work.iterdir()):
        if not marker.exists():
            print('Refusing to reuse a non-empty directory not created by this runner: %s' % work, file=sys.stderr)
            return 2
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.write_text('owned by run_maintenance.py\n')
    for sub in ('state', 'scratch', 'secrets', 'home'):
        (work / sub).mkdir(mode=0o700)
    token_file = work / 'secrets' / 'claude-oauth'
    publish = not args.repair_probe
    cfg = build_config(policy, seed_repo='', seed_sha='', state_dir=str(state_dir), runtime=runtime,
                       scratch_root=str(work / 'scratch'), claude_bin=args.claude_bin, token_file=str(token_file))
    secrets = [t for t in (claude_token,) if t]
    try:
        if not claude_token:
            raise RuntimeError('CLAUDE_CODE_OAUTH_TOKEN is not available to this run; no API-billing fallback')
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(claude_token)
        probe_root = None
        if args.repair_probe:
            probe_root = Path(tempfile.mkdtemp(prefix='probe-', dir=work))
            fixture = build_probe_fixture(probe_root)
            probe_cfg = build_config(probe_policy(fixture), seed_repo=fixture['seed'], seed_sha=fixture['seed_sha'],
                                     state_dir=str(state_dir), runtime=runtime,
                                     scratch_root=str(work / 'scratch'), claude_bin=args.claude_bin,
                                     token_file=str(token_file))
            probe_cfg['test_command'] = [sys.executable, str(ROOT / 'probe_check.py'), '{candidate}']
            cfg = probe_cfg
            publish = False
            github_token = ''
        else:
            cfg['seed_repo'] = str(args.seed_repo.resolve(strict=True))
            cfg['seed_sha'] = seed_identity(cfg['seed_repo'], policy['deployment_branch'])
        env, more = maintainer_env(str(work / 'home'), str(work / 'scratch'), github_token, cfg['fork_url'])
        secrets += more
        for s in more:
            print('::add-mask::' + s)
        config_path = work / 'config.json'
        config_path.write_text(json.dumps(cfg, indent=2))
        if not args.repair_probe:
            sandbox_self_test(runtime, str(work / 'scratch'))
        argv_m = [sys.executable, str(ROOT / 'maintainer.py'), '--config', str(config_path)]
        if publish:
            argv_m.append('--publish')
        rc = run_group(argv_m, env, str(work), MAINTAINER_DEADLINE)
    except Exception as exc:
        reason = str(exc)
        for s in secrets:
            reason = reason.replace(s, '[redacted]')
        if not cfg.get('seed_sha'):
            cfg['seed_repo'] = cfg['seed_repo'] or str(args.seed_repo or '')
        write_atomic(state_dir / 'latest.json', failure_doc(cfg, 'runner error: ' + reason, started, publish))
        print('runner error: ' + reason, file=sys.stderr)
        shutil.rmtree(work / 'secrets', ignore_errors=True)
        return 1
    finally:
        shutil.rmtree(work / 'secrets', ignore_errors=True)
    doc = finalize_latest(cfg, started, publish, rc, secrets)
    if args.repair_probe and rc == 0:
        problem = evaluate_probe(doc)
        if problem:
            print('repair probe failed: ' + problem, file=sys.stderr)
            return 1
        print('repair probe passed: real Claude response resolved the synthetic conflict')
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
