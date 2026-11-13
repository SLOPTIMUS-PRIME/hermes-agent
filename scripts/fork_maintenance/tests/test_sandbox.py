"""Sandbox adapter: argv construction always; a real Bubblewrap isolation probe when the host permits it."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prepare_runtime  # noqa: E402
import sandbox_tests as s  # noqa: E402

INFO = {'python': '/python/bin/python3', 'prefix': '/python', 'site': '/venv/site-packages'}


def host_can_mount_proc() -> bool:
    try:
        bwrap = s.find_bwrap()
    except SystemExit:
        return False
    r = subprocess.run([bwrap, '--unshare-all', '--unshare-user', '--cap-drop', 'ALL', '--ro-bind', '/usr', '/usr',
                        '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64', '--proc', '/proc',
                        '/usr/bin/true'], capture_output=True, timeout=30)
    return r.returncode == 0


class Argv(unittest.TestCase):
    def cmd(self, **kw):
        return s.command(Path('/candidate-source'), Path('/scratch'), INFO, 'pass', **kw)

    def pairs(self, cmd):
        return [tuple(cmd[i:i + 3]) for i in range(len(cmd))]

    def test_private_proc_unshared_namespaces_and_no_capabilities(self):
        cmd = self.cmd()
        self.assertIn('--unshare-all', cmd)
        self.assertEqual(cmd[cmd.index('--proc') + 1], '/proc')
        self.assertNotIn(('--ro-bind', '/proc', '/proc'), self.pairs(cmd))
        self.assertNotIn(('--bind', '/proc', '/proc'), self.pairs(cmd))
        self.assertEqual(cmd[cmd.index('--cap-drop') + 1], 'ALL')
        self.assertIn('--clearenv', cmd)
        self.assertNotIn('--share-net', cmd)
        self.assertNotIn('--dev-bind', cmd)

    def test_only_scratch_is_writable_and_candidate_is_read_only(self):
        cmd = self.cmd()
        writable = [cmd[i + 1:i + 3] for i, a in enumerate(cmd) if a in ('--bind', '--bind-try')]
        self.assertEqual(writable, [['/scratch', '/work']])
        self.assertIn(('--ro-bind', '/candidate-source', '/candidate'), self.pairs(cmd))
        self.assertIn(('--ro-bind', '/venv/site-packages', '/deps'), self.pairs(cmd))
        self.assertNotIn('/home', cmd)

    def test_userns_lockdown_is_optional_but_applied_when_requested(self):
        self.assertNotIn('--disable-userns', self.cmd())
        self.assertIn('--disable-userns', self.cmd(disable_userns=True))

    def test_required_regression_tests_are_the_upstream_set(self):
        self.assertEqual(len(s.DEFAULT_TESTS), 5)
        self.assertEqual(set(s.DEFAULT_TESTS), {
            'tests/agent/test_curator_conservative_prompt.py', 'tests/tui_gateway/test_queued_prompt_stop_recovery.py',
            'tests/tui_gateway/test_queued_prompt_persistence.py', 'tests/agent/test_curator.py',
            'tests/hermes_cli/test_model_allowlist.py'})
        payload = s.pytest_payload()
        for rel in s.DEFAULT_TESTS:
            self.assertIn(rel, payload)
        self.assertNotIn('--deselect', payload)
        self.assertNotIn('-k', payload)

    def test_host_environment_never_reaches_bubblewrap(self):
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'x', 'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic x'}), \
                mock.patch.object(s, 'find_bwrap', return_value='/usr/bin/bwrap'), \
                mock.patch.object(s, 'supports_disable_userns', return_value=False), \
                mock.patch.object(s.subprocess, 'run') as run:
            s.sandbox(Path('/c'), Path('/s'), INFO, 'pass', 5)
        self.assertEqual(run.call_args.kwargs['env'], s.HOST_ENV)

    def test_missing_required_test_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Path(tmp) / 'runtime.json'
            rt.write_text(json.dumps({'python': sys.executable, 'prefix': sys.prefix, 'site': tmp}))
            with mock.patch.object(s, 'run_probe', return_value=0), self.assertRaises(SystemExit) as ctx:
                s.main(['--runtime', str(rt), tmp])
            self.assertIn('Missing required regression test', str(ctx.exception))

    def test_invalid_runtime_descriptions_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Path(tmp) / 'runtime.json'
            for doc in ({'python': 'python', 'prefix': '/usr', 'site': '/usr'}, {'python': '/nope/p', 'prefix': '/nope', 'site': '/nope'}):
                rt.write_text(json.dumps(doc))
                with self.assertRaises(SystemExit):
                    s.load_runtime(rt)


class RealSandbox(unittest.TestCase):
    """Runs the actual probe against a real bwrap.  Skipped, loudly, where the host forbids it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.runtime = Path(cls.tmp.name) / 'runtime.json'
        venv = Path(cls.tmp.name) / 'venv'
        site = Path(cls.tmp.name) / 'site'
        site.mkdir()
        cls.info = {'python': os.path.realpath(getattr(sys, '_base_executable', sys.executable)),
                    'prefix': os.path.realpath(sys.base_prefix), 'site': str(site)}
        cls.runtime.write_text(json.dumps(cls.info))
        del venv

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self):
        return subprocess.run([sys.executable, str(ROOT / 'sandbox_tests.py'),
            '--runtime', str(self.runtime), '--scratch-root', self.tmp.name, '--self-test'],
            capture_output=True, text=True, timeout=120)

    def test_isolation_probe_passes_and_detects_nothing_leaky(self):
        self.assertTrue(host_can_mount_proc(), 'Run these tests in a service/runner that permits private procfs')
        r = self.probe()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Sandbox: ', r.stdout)

    def test_sandbox_blocks_network_source_write_and_credential_environment(self):
        try:
            bwrap = s.find_bwrap()
        except SystemExit:
            self.skipTest('bubblewrap is not installed')
        self.assertTrue(host_can_mount_proc(), 'Private procfs is required')
        with tempfile.TemporaryDirectory() as cand, tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / 'home').mkdir()
            (Path(scratch) / 'tmp').mkdir()
            payload = s.PREFIX + (
                'import os, pathlib, socket\n'
                'out = {}\n'
                'out["env"] = sorted(os.environ)\n'
                'try: pathlib.Path("/candidate/x").write_text("1"); out["write"] = True\n'
                'except OSError: out["write"] = False\n'
                'try: socket.create_connection(("1.1.1.1", 443), timeout=1); out["net"] = True\n'
                'except OSError: out["net"] = False\n'
                'print("RESULT" + __import__("json").dumps(out))\n')
            cmd = s.command(Path(cand), Path(scratch), self.info, payload, bwrap)
            with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'leak', 'CLAUDE_CODE_OAUTH_TOKEN': 'leak'}):
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=s.HOST_ENV)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout.split('RESULT', 1)[1])
            self.assertFalse(out['write'])
            self.assertFalse(out['net'])
            self.assertFalse([k for k in out['env'] if 'TOKEN' in k or k.startswith('GITHUB')])


class PrepareRuntime(unittest.TestCase):
    def test_describe_rejects_a_venv_without_test_tooling(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / 'v'
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True, capture_output=True)
            with self.assertRaises(SystemExit) as ctx:
                prepare_runtime.describe(venv)
            self.assertRegex(str(ctx.exception), 'Test tooling missing|Unsafe or missing runtime location')


if __name__ == '__main__':
    unittest.main()
