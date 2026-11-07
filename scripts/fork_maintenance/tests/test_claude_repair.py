"""Claude Code repair adapter: argv, environment isolation and bounded response validation."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import claude_repair as c  # noqa: E402
import repair_protocol as protocol  # noqa: E402

TOKEN = 'sk-ant-oat01-unit-test-token'
RESOLVED = {'status': 'resolved', 'reason': 'kept intent', 'edits': [{'path': 'agent/curator.py', 'content': 'x = 1\n'}]}


def cli_ok(result) -> str:
    return json.dumps({'type': 'result', 'is_error': False, 'result': result if isinstance(result, str) else json.dumps(result)})


class FakeClaude:
    """Executable that records exactly what the adapter gave it and replies from files."""

    def __init__(self, directory: Path, stdout: str, rc: int = 0):
        self.dir = directory
        self.bin = directory / 'claude'
        (directory / 'stdout').write_text(stdout)
        (directory / 'rc').write_text(str(rc))
        self.bin.write_text(
            '#!%s\nimport json, os, pathlib, sys\nhere = pathlib.Path(__file__).parent\n'
            'stdin = sys.stdin.read()\n'
            '(here / "seen.json").write_text(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ), '
            '"cwd": os.getcwd(), "stdin": stdin}))\n'
            'sys.stdout.write((here / "stdout").read_text())\n'
            'sys.exit(int((here / "rc").read_text()))\n' % sys.executable)
        self.bin.chmod(0o700)

    def seen(self):
        path = self.dir / 'seen.json'
        return json.loads(path.read_text()) if path.exists() else None


class CommandAndParsing(unittest.TestCase):
    def test_command_has_no_tools_or_project_customizations(self):
        cmd = c.command('/opt/claude')
        self.assertEqual(cmd[0], '/opt/claude')
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertEqual(cmd[cmd.index('--mcp-config') + 1], '{"mcpServers":{}}')
        self.assertEqual(cmd[cmd.index('--setting-sources') + 1], '')
        self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(cmd[cmd.index('--model') + 1], 'sonnet')
        self.assertEqual(cmd[cmd.index('--output-format') + 1], 'json')
        for flag in ('--safe-mode', '--strict-mcp-config', '--no-session-persistence'):
            self.assertIn(flag, cmd)
        self.assertNotIn('--bare', cmd)  # --bare forbids subscription OAuth
        self.assertNotIn('--dangerously-skip-permissions', cmd)

    def test_child_env_is_exactly_the_minimal_set(self):
        env = c.child_env('/tmp/x', TOKEN)
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], TOKEN)
        self.assertEqual(env['HOME'], '/tmp/x')
        self.assertFalse([k for k in env if k.startswith(('GITHUB', 'GIT_', 'ANTHROPIC', 'GH_'))])

    def test_failed_cli_response_is_not_accepted(self):
        for raw in (json.dumps({'is_error': True, 'result': 'failed'}), json.dumps({'result': 'x'}),
                    json.dumps({'is_error': False, 'result': None}), '[]', 'not json'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                c.parse_cli_response(raw)

    def test_valid_and_fenced_repair_results(self):
        self.assertEqual(c.parse_cli_response(cli_ok(RESOLVED)), RESOLVED)
        self.assertEqual(c.parse_cli_response(cli_ok('```json\n' + json.dumps(RESOLVED) + '\n```')), RESOLVED)

    def test_protocol_rejects_unsafe_or_malformed_models(self):
        bad = ['Done', '[]', '{"status":"resolved","edits":[]}', '{"status":"unknown","reason":"x","edits":[]}',
               '{"status":"blocked","reason":"no","edits":[{"path":"x","content":"x"}]}',
               '{"status":"resolved","reason":"r","edits":[{"path":"x","content":"x","mode":"0777"}]}',
               '{"status":"resolved","reason":"r","edits":[{"path":"x","content":3}]}',
               'x' * (protocol.MAX_OUTPUT + 1)]
        for raw in bad:
            with self.subTest(raw=raw[:40]), self.assertRaises((ValueError, TypeError)):
                protocol.parse_response(raw)


class AdapterProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.token_file = self.dir / 'token'
        self.token_file.write_text(TOKEN + '\n')
        self.token_file.chmod(0o600)
        (self.dir / 'bin').mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_adapter(self, fake_stdout, rc=0, token=TOKEN, payload=None):
        fake = FakeClaude(self.dir / 'bin', fake_stdout, rc)
        self.token_file.write_text(token)
        ambient = dict(os.environ, GITHUB_TOKEN='ghs_ambient', GH_TOKEN='ghs_ambient2', ANTHROPIC_API_KEY='sk-ant-api03-ambient',
                       CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat01-ambient', GIT_CONFIG_COUNT='1',
                       GIT_CONFIG_VALUE_0='AUTHORIZATION: basic QUJD', ACTIONS_RUNTIME_TOKEN='rt')
        r = subprocess.run([sys.executable, str(ROOT / 'claude_repair.py'), '--claude-bin', str(fake.bin),
                            '--token-file', str(self.token_file)], input=json.dumps(payload or {'schema': 1, 'conflicts': []}),
                           text=True, capture_output=True, env=ambient, timeout=60)
        return r, fake

    def test_claude_sees_only_oauth_and_private_dirs(self):
        r, fake = self.run_adapter(cli_ok(RESOLVED), payload={'schema': 1, 'note': 'ünï'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), RESOLVED)
        seen = fake.seen()
        assert seen is not None
        # maintainer.run_cmd adds three inert constants; nothing else may be inherited.
        inert = {'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/true', 'LC_ALL': 'C'}
        self.assertEqual({k: v for k, v in seen['env'].items() if k not in inert}, c.child_env(seen['cwd'], TOKEN))
        self.assertEqual(seen['env']['HOME'], seen['cwd'])
        self.assertNotEqual(Path(seen['cwd']), Path.cwd())
        self.assertEqual(json.loads(seen['stdin']), {'schema': 1, 'note': 'ünï'})
        self.assertEqual(seen['argv'], c.command(str(fake.bin))[1:])
        self.assertFalse(Path(seen['cwd']).exists(), 'temporary Claude home is removed')

    def test_api_key_credentials_are_refused_without_running_claude(self):
        r, fake = self.run_adapter(cli_ok(RESOLVED), token='sk-ant-api03-not-subscription')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)['status'], 'blocked')
        self.assertIsNone(fake.seen())

    def test_cli_error_is_blocked_and_never_echoes_the_token(self):
        out = json.dumps({'is_error': True, 'result': 'authentication failed for ' + TOKEN})
        r, _ = self.run_adapter(out, rc=1)
        self.assertEqual(r.returncode, 1)
        doc = json.loads(r.stdout)
        self.assertEqual((doc['status'], doc['edits']), ('blocked', []))
        self.assertIn('authentication failed', doc['reason'])
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_model_text_that_is_not_the_protocol_is_blocked(self):
        for result in ('I resolved it.', json.dumps({'status': 'resolved', 'reason': 'x', 'edits': 'no'})):
            with self.subTest(result=result):
                r, _ = self.run_adapter(cli_ok(result))
                self.assertEqual(r.returncode, 1)
                self.assertEqual(json.loads(r.stdout)['status'], 'blocked')

    def test_missing_or_nonexecutable_binary_is_blocked(self):
        self.token_file.write_text(TOKEN)
        for binary in (str(self.dir / 'absent'), 'claude'):
            r = subprocess.run([sys.executable, str(ROOT / 'claude_repair.py'), '--claude-bin', binary,
                                '--token-file', str(self.token_file)], input='{}', text=True, capture_output=True)
            self.assertEqual(r.returncode, 1)
            self.assertEqual(json.loads(r.stdout)['status'], 'blocked')

    def test_oversized_input_is_rejected(self):
        fake = FakeClaude(self.dir / 'bin', cli_ok(RESOLVED))
        r = subprocess.run([sys.executable, str(ROOT / 'claude_repair.py'), '--claude-bin', str(fake.bin),
                            '--token-file', str(self.token_file)], input='{"x":"' + 'a' * protocol.MAX_INPUT + '"}',
                           text=True, capture_output=True)
        self.assertEqual(r.returncode, 1)
        self.assertIsNone(fake.seen())

    def test_symlinked_token_file_is_refused(self):
        link = self.dir / 'link'
        link.symlink_to(self.token_file)
        with self.assertRaises(ValueError):
            c.read_token(str(link))


if __name__ == '__main__':
    unittest.main()
