#!/usr/bin/env python3
"""No-tools Claude Code CLI repair adapter for the GitHub Actions fork maintainer.

Protocol: one JSON object on stdin, one JSON object on stdout
({status: resolved|blocked, reason, edits:[{path, content}]}).  The CLI runs with
no tools, no MCP, no settings, no session persistence, in an empty temporary
directory with a fresh HOME and an environment that contains only the Claude
subscription OAuth token and a few harmless variables.  No GitHub credential is
ever present in this process tree: the token is read from a private file whose
path is given on argv, never from the inherited environment.  There is no API-key
fallback; --bare is deliberately not used because it forbids subscription OAuth.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from repair_protocol import MAX_INPUT, MAX_OUTPUT, SYSTEM, parse_response  # noqa: E402
from maintainer import run_cmd  # noqa: E402

DEADLINE_SECONDS = 420
MAX_TOKEN_FILE = 4096
OAUTH_PREFIX = 'sk-ant-oat'


def command(claude_bin: str) -> list[str]:
    return [claude_bin, '-p', '--safe-mode',
            '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', '', '--no-session-persistence',
            '--disable-slash-commands', '--no-chrome', '--permission-mode', 'dontAsk',
            '--output-format', 'json', '--model', 'sonnet', '--system-prompt', SYSTEM]


def child_env(tmp: str, token: str) -> dict[str, str]:
    """The complete environment of the Claude process; nothing is inherited."""
    return {'HOME': tmp, 'PATH': '/usr/bin:/bin', 'TMPDIR': tmp, 'CLAUDE_CONFIG_DIR': tmp,
            'LANG': 'C.UTF-8', 'CLAUDE_CODE_OAUTH_TOKEN': token,
            'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1',
            'DISABLE_AUTOUPDATER': '1',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
            'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'}


def read_token(path: str) -> str:
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise ValueError('OAuth token file is missing')
    raw = p.read_bytes()
    if len(raw) > MAX_TOKEN_FILE:
        raise ValueError('OAuth token file is oversized')
    token = raw.decode('utf-8', 'strict').strip()
    if not token.startswith(OAUTH_PREFIX) or any(c.isspace() for c in token):
        raise ValueError('Claude Code requires the subscription OAuth credential; no API billing fallback')
    return token


def parse_cli_response(raw: str) -> dict:
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('is_error') is not False:
        raise ValueError('Claude Code did not complete the repair')
    if not isinstance(result.get('result'), str):
        raise ValueError('Claude Code returned no text result')
    return parse_response(result['result'])


def redact(text: str, token: str) -> str:
    return text.replace(token, '[redacted]')[:300] if token else text[:300]


def repair(payload: dict, claude_bin: str, token: str, scratch_root: str | None = None) -> dict:
    if not os.path.isabs(claude_bin) or not os.access(claude_bin, os.X_OK):
        raise ValueError('Claude Code binary is not an executable absolute path')
    with tempfile.TemporaryDirectory(prefix='fork-claude-', dir=scratch_root) as tmp:
        original = dict(os.environ)
        try:
            # run_cmd merges os.environ into the child: empty it for the call.
            os.environ.clear()
            os.environ.update(child_env(tmp, token))
            result = run_cmd(command(claude_bin), cwd=tmp, timeout=DEADLINE_SECONDS,
                             stdin_text=json.dumps(payload, ensure_ascii=False),
                             capture_bytes=MAX_OUTPUT + 20000)
        finally:
            os.environ.clear()
            os.environ.update(original)
    if result.timed_out:
        raise RuntimeError('Claude Code repair deadline exceeded')
    if result.rc:
        detail = ''
        try:
            doc = json.loads(result.out)
            if isinstance(doc, dict) and isinstance(doc.get('result'), str):
                detail = ': ' + redact(doc['result'], token)
        except ValueError:
            pass
        raise RuntimeError('Claude Code repair failed (exit ' + str(result.rc) + ')' + detail)
    return parse_cli_response(result.out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--claude-bin', required=True)
    ap.add_argument('--token-file', required=True)
    args = ap.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError('Repair input exceeds limit')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('Repair input must be an object')
    response = repair(payload, args.claude_bin, read_token(args.token_file))
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'reason': 'Claude Code repair failed: ' + str(exc)[:400], 'edits': []}))
        raise SystemExit(1)
