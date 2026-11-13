"""Runner: config, credential routing, result guarantees and real-git end-to-end runs (no network, no model)."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import maintainer  # noqa: E402
import run_maintenance as rm  # noqa: E402
from test_claude_repair import FakeClaude, cli_ok  # noqa: E402

OAUTH = 'sk-ant-oat01-runner-test'
GH = 'ghs_runnerTestToken123'
IDENT = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 't',
         'GIT_COMMITTER_EMAIL': 't@example.invalid'}
RESOLVED_PROBE = 'DESCRIPTION_LIMIT = 60\nCONSERVATIVE_CURATION = True\n\n\ndef describe():\n    return "limit %d" % DESCRIPTION_LIMIT\n'
LOCAL_CONFIG = Path('/home/nick/workspace/hermes-fork-maintainer/config.json')
LOCAL_MAINTAINER = Path('/home/nick/workspace/hermes-fork-maintainer/maintainer.py')


def commit_file(repo: str, rel: str, text: str, msg: str):
    path = Path(repo) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    rm.git('add', rel, cwd=repo)
    rm.git('commit', '-qm', msg, cwd=repo, env=IDENT)


def clean_fixture(root: Path) -> dict:
    """Real repos: upstream adds an unrelated file, the fork patch edits agent/curator.py."""
    work = str(root / 'work-src')
    os.mkdir(work)
    rm.git('init', '-q', '-b', 'main', cwd=work)
    commit_file(work, 'agent/curator.py', 'LIMIT = 57\n', 'base')
    rm.git('checkout', '-q', '-b', 'nick-patches', cwd=work)
    commit_file(work, 'agent/curator.py', 'LIMIT = 57\nCONSERVATIVE = True\n', 'fork patch')
    rm.git('checkout', '-q', 'main', cwd=work)
    commit_file(work, 'README.txt', 'upstream\n', 'upstream work')
    for bare, branch in (('official.git', 'main'), ('fork.git', 'nick-patches')):
        rm.git('init', '-q', '--bare', str(root / bare), cwd=str(root))
        rm.git('push', '-q', str(root / bare), 'refs/heads/%s:refs/heads/%s' % (branch, branch), cwd=work)
    seed = str(root / 'seed')
    rm.git('clone', '-q', '-b', 'nick-patches', str(root / 'fork.git'), seed, cwd=str(root))
    return {'official': str(root / 'official.git'), 'fork': str(root / 'fork.git'), 'seed': seed}


def local_policy(fixture: dict, **over) -> dict:
    policy = {'official_url': fixture['official'], 'official_branch': 'main', 'fork_url': fixture['fork'],
              'deployment_branch': 'nick-patches', 'allowed_edit_paths': ['agent/curator.py'],
              'patch_intents': {'agent/curator.py': 'keep CONSERVATIVE'}, 'max_repairs': 1, 'max_input_bytes': 750000,
              'max_edit_bytes': 300000, 'reuse_validation': False, 'command_timeout': 120, 'test_timeout': 120}
    policy.update(over)
    return policy


class Config(unittest.TestCase):
    def test_production_policy_matches_the_tested_local_contract(self):
        policy = rm.load_policy()
        self.assertEqual(policy['allowed_edit_paths'], ['agent/curator.py', 'tui_gateway/session_auto_continue.py',
                                                        'hermes_cli/inventory.py', 'hermes_cli/model_switch_providers.py'])
        self.assertEqual(set(policy['patch_intents']), set(policy['allowed_edit_paths']))
        self.assertEqual(policy['fork_url'], 'https://github.com/SLOPTIMUS-PRIME/hermes-agent.git')
        self.assertEqual(policy['official_url'], 'https://github.com/NousResearch/hermes-agent.git')
        self.assertEqual(policy['deployment_branch'], 'main')
        self.assertIs(policy['reuse_validation'], False)
        if LOCAL_CONFIG.exists():
            local = json.loads(LOCAL_CONFIG.read_text())
            for key in ('allowed_edit_paths', 'patch_intents', 'official_url', 'fork_url', 'max_repairs',
                        'max_input_bytes', 'max_edit_bytes'):
                self.assertEqual(policy[key], local[key], key)

    def test_generated_config_is_accepted_by_the_unmodified_maintainer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = rm.build_config(rm.load_policy(), seed_repo='/seed', seed_sha='a' * 40, state_dir=tmp + '/state',
                                  runtime='/rt.json', scratch_root=tmp + '/scratch', claude_bin='/bin/claude',
                                  token_file='/t')
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(cfg))
            loaded, err = maintainer.load_config(str(path))
        self.assertIsNone(err)
        self.assertEqual(loaded['allowed'], {'agent/curator.py', 'tui_gateway/session_auto_continue.py',
                                             'hermes_cli/inventory.py', 'hermes_cli/model_switch_providers.py'})
        self.assertIn('.github', loaded['forbidden'])
        self.assertIn('{candidate}', cfg['test_command'])
        self.assertEqual(cfg['test_command'][1], str(ROOT / 'sandbox_tests.py'))
        self.assertEqual(cfg['repair_command'][1], str(ROOT / 'claude_repair.py'))
        self.assertNotIn('/home/nick', (ROOT / 'policy.json').read_text())
        self.assertNotIn('check_command', cfg)

    def test_maintainer_core_is_the_unmodified_tested_copy(self):
        if not LOCAL_MAINTAINER.exists():
            self.skipTest('local maintainer source is not present')
        self.assertEqual((ROOT / 'maintainer.py').read_bytes(), LOCAL_MAINTAINER.read_bytes())


class Credentials(unittest.TestCase):
    def test_env_contains_github_credentials_only_when_publishing(self):
        env, secrets = rm.maintainer_env('/h', '/s', GH, 'https://github.com/o/r.git')
        self.assertEqual(len(secrets), 2)
        self.assertEqual(env['GIT_CONFIG_KEY_0'], 'http.https://github.com/o/r.git.extraheader')
        self.assertNotIn(GH, json.dumps(env))  # only its base64 header form is passed
        self.assertTrue(env['GIT_CONFIG_VALUE_0'].startswith('AUTHORIZATION: basic '))
        env, secrets = rm.maintainer_env('/h', '/s', '', 'https://github.com/o/r.git')
        self.assertEqual(secrets, [])
        self.assertFalse([k for k in env if k.startswith('GIT_CONFIG_KEY') or 'TOKEN' in k])
        self.assertEqual(env['HOME'], '/h')
        self.assertEqual(set(env) - {'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_TERMINAL_PROMPT'},
                         {'PATH', 'HOME', 'TMPDIR', 'LANG'})

    def test_authorization_header_is_sent_only_to_the_fork_url(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('Authorization')))
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = 'http://127.0.0.1:%d' % server.server_port
            fork_url = base + '/fork/hermes.git'
            extra, _ = rm.git_publish_env(GH, fork_url)
            env = dict(os.environ, **extra, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
            for url in (fork_url, base + '/upstream/hermes.git', base + '/fork/hermes.git.evil'):
                subprocess.run(['git', 'ls-remote', url], env=env, capture_output=True, timeout=30)
        finally:
            server.shutdown()
            server.server_close()
        by_path = {p.split('/info')[0]: a for p, a in seen}
        self.assertTrue(by_path['/fork/hermes.git'].startswith('basic '))
        self.assertIsNone(by_path['/upstream/hermes.git'])
        self.assertIsNone(by_path.get('/fork/hermes.git.evil'))


class Results(unittest.TestCase):
    def cfg(self, tmp):
        return rm.build_config(rm.load_policy(), seed_repo='/seed', seed_sha='b' * 40, state_dir=tmp,
                               runtime='', scratch_root='', claude_bin='', token_file='')

    def test_missing_result_becomes_a_schema_compatible_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = rm.finalize_latest(self.cfg(tmp), '2026-01-01T00:00:00Z', True, 7, [])
            on_disk = json.loads((Path(tmp) / 'latest.json').read_text())
        self.assertEqual(doc, on_disk)
        self.assertEqual((doc['status'], doc['exit_code'], doc['candidate']), ('failed', 7, None))
        self.assertEqual(doc['input']['fork_url'], 'https://github.com/SLOPTIMUS-PRIME/hermes-agent.git')
        self.assertEqual(doc['input']['deployment_branch'], 'main')
        self.assertNotEqual(doc['publish']['status'], 'published')
        self.assertIsNone(doc['checks']['tests'])

    def test_valid_maintainer_result_is_kept_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.dumps({'schema': 1, 'status': 'no-op', 'x': [1]}, indent=2) + '\n'
            (Path(tmp) / 'latest.json').write_text(raw)
            rm.finalize_latest(self.cfg(tmp), 's', True, 0, [])
            self.assertEqual((Path(tmp) / 'latest.json').read_text(), raw)

    def test_result_with_credential_material_is_withheld(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'latest.json').write_text(json.dumps({'schema': 1, 'status': 'published', 'leak': GH}))
            doc = rm.finalize_latest(self.cfg(tmp), 's', True, 0, [GH])
            self.assertEqual(doc['status'], 'failed')
            self.assertNotIn(GH, (Path(tmp) / 'latest.json').read_text())

    def test_garbage_and_wrong_schema_results_fail_closed(self):
        for raw in ('{', '[]', json.dumps({'schema': 2, 'status': 'published'}), json.dumps({'schema': 1})):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / 'latest.json').write_text(raw)
                self.assertEqual(rm.finalize_latest(self.cfg(tmp), 's', True, 0, [])['status'], 'failed')

    def test_oversized_result_drops_diagnostics_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = {'schema': 1, 'status': 'published', 'candidate': 'c' * 40, 'events': ['e' * 1000] * 1000,
                   'repairs': {'results': ['r' * 1000] * 10, 'attempts': 0}}
            (Path(tmp) / 'latest.json').write_text(json.dumps(doc))
            out = rm.finalize_latest(self.cfg(tmp), 's', True, 0, [])
            self.assertEqual((out['status'], out['candidate'], out['events']), ('published', 'c' * 40, []))
            self.assertLess((Path(tmp) / 'latest.json').stat().st_size, rm.MAX_LATEST_BYTES)

    def test_ensure_result_only_fills_a_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            self.assertEqual(rm.main(['--work-dir', str(work), '--ensure-result']), 0)
            first = (work / 'state' / 'latest.json').read_text()
            self.assertEqual(json.loads(first)['status'], 'failed')
            (work / 'state' / 'latest.json').write_text('{"schema": 1, "status": "no-op"}')
            rm.main(['--work-dir', str(work), '--ensure-result'])
            self.assertEqual(json.loads((work / 'state' / 'latest.json').read_text())['status'], 'no-op')

    def test_runner_refuses_to_reuse_an_unowned_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'precious').write_text('x')
            with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': OAUTH}):
                rc = rm.main(['--work-dir', tmp, '--claude-bin', '/bin/true', '--repair-probe'])
            self.assertEqual(rc, 2)
            self.assertTrue((Path(tmp) / 'precious').exists())

    def test_missing_claude_token_fails_without_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CODE_OAUTH_TOKEN'}
            env['ANTHROPIC_API_KEY'] = 'sk-ant-api03-must-not-be-used'
            with mock.patch.dict(os.environ, env, clear=True):
                rc = rm.main(['--work-dir', tmp + '/w', '--claude-bin', '/bin/true', '--repair-probe'])
            doc = json.loads((Path(tmp) / 'w' / 'state' / 'latest.json').read_text())
        self.assertEqual(rc, 1)
        self.assertEqual(doc['status'], 'failed')
        self.assertIn('no API-billing fallback', doc['reason'])


class SeedIdentity(unittest.TestCase):
    def test_seed_must_be_the_deployment_branch_at_a_full_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = clean_fixture(Path(tmp))
            sha = rm.seed_identity(fixture['seed'], 'nick-patches')
            self.assertRegex(sha, r'^[0-9a-f]{40}$')
            rm.git('checkout', '-q', '-b', 'other', cwd=fixture['seed'])
            with self.assertRaises(RuntimeError):
                rm.seed_identity(fixture['seed'], 'nick-patches')


class EndToEnd(unittest.TestCase):
    """Whole runner + unmodified maintainer + Claude adapter against real local repositories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_claude(self, stdout, rc=0):
        (self.root / 'bin').mkdir(exist_ok=True)
        return FakeClaude(self.root / 'bin', stdout, rc)

    def write_tests_script(self, exit_code):
        script = self.root / 'fake_tests.py'
        script.write_text('import sys\nsys.exit(%d)\n' % exit_code)
        return str(script)

    def run_main(self, fixture, claude_bin, tests_exit=0, work='work'):
        policy = local_policy(fixture)
        real_build = rm.build_config

        def build(pol, **kw):
            cfg = real_build(pol, **kw)
            cfg['test_command'] = [sys.executable, self.write_tests_script(tests_exit), '{candidate}']
            return cfg

        env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=OAUTH, GITHUB_TOKEN=GH)
        with mock.patch.dict(os.environ, env), mock.patch.object(rm, 'load_policy', return_value=policy), \
                mock.patch.object(rm, 'build_config', side_effect=build), \
                mock.patch.object(rm, 'sandbox_self_test') as selftest:
            rc = rm.main(['--work-dir', str(self.root / work), '--seed-repo', fixture['seed'],
                          '--runtime', str(self.root / 'rt.json'), '--claude-bin', claude_bin])
        selftest.assert_called_once()
        latest = json.loads((self.root / work / 'state' / 'latest.json').read_text())
        return rc, latest

    def remote_sha(self, fixture):
        return rm.git('rev-parse', 'refs/heads/nick-patches', cwd=fixture['fork'])

    def test_clean_update_publishes_then_second_run_is_noop(self):
        fixture = clean_fixture(self.root)
        before = self.remote_sha(fixture)
        claude = self.make_claude(cli_ok({'status': 'blocked', 'reason': 'should not be called', 'edits': []}))
        rc, latest = self.run_main(fixture, str(claude.bin))
        self.assertEqual(rc, 0, latest)
        after = self.remote_sha(fixture)
        self.assertNotEqual(after, before)
        self.assertEqual((latest['status'], latest['candidate']), ('published', after))
        self.assertEqual(latest['checks']['tests']['status'], 'pass')
        self.assertEqual((latest['publish']['status'], latest['publish']['remote_after']), ('published', after))
        self.assertEqual(latest['publish']['expected_old'], before)
        self.assertEqual(latest['input']['deployment_branch'], 'nick-patches')
        self.assertEqual(latest['repairs']['attempts'], 0)
        self.assertIsNone(claude.seen(), 'Claude is not invoked when there is no conflict')
        self.assertFalse((self.root / 'work' / 'secrets').exists())
        self.assertNotIn(OAUTH, json.dumps(latest))
        self.assertNotIn(GH, json.dumps(latest))
        # Fresh runner: the checked-out seed is now the published head, nothing left to do.
        rm.git('pull', '-q', '--ff-only', cwd=fixture['seed'])
        rc, latest = self.run_main(fixture, str(claude.bin), work='work2')
        self.assertEqual(rc, 0)
        self.assertEqual((latest['status'], latest['candidate']), ('no-op', after))
        self.assertEqual((latest['publish']['status'], latest['publish']['remote_after']), ('skipped-no-op', after))
        self.assertEqual(latest['checks']['tests']['status'], 'pass')
        self.assertEqual(self.remote_sha(fixture), after)

    def test_failing_required_tests_never_publish(self):
        fixture = clean_fixture(self.root)
        before = self.remote_sha(fixture)
        claude = self.make_claude(cli_ok({'status': 'blocked', 'reason': 'cannot fix', 'edits': []}))
        rc, latest = self.run_main(fixture, str(claude.bin), tests_exit=1)
        self.assertNotEqual(rc, 0)
        self.assertIn(latest['status'], ('blocked', 'failed'))
        self.assertEqual(latest['checks']['tests']['status'], 'fail')
        self.assertNotEqual(latest['publish']['status'], 'published')
        self.assertEqual(self.remote_sha(fixture), before)
        self.assertEqual(latest['repairs']['attempts'], 1)

    def test_runner_error_still_leaves_a_failure_result(self):
        fixture = clean_fixture(self.root)
        rm.git('checkout', '-q', '-b', 'wrong', cwd=fixture['seed'])
        claude = self.make_claude(cli_ok(''))
        policy = local_policy(fixture)
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': OAUTH, 'GITHUB_TOKEN': GH}), \
                mock.patch.object(rm, 'load_policy', return_value=policy):
            rc = rm.main(['--work-dir', str(self.root / 'w'), '--seed-repo', fixture['seed'],
                          '--runtime', str(self.root / 'rt.json'), '--claude-bin', str(claude.bin)])
        latest = json.loads((self.root / 'w' / 'state' / 'latest.json').read_text())
        self.assertEqual(rc, 1)
        self.assertEqual(latest['status'], 'failed')
        self.assertIn('not the nick-patches branch', latest['reason'])

    def probe(self, claude_bin):
        env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=OAUTH, GITHUB_TOKEN=GH)
        r = subprocess.run([sys.executable, str(ROOT / 'run_maintenance.py'), '--work-dir', str(self.root / 'probe'),
                            '--claude-bin', claude_bin, '--repair-probe'], env=env, capture_output=True, text=True, timeout=240)
        latest = json.loads((self.root / 'probe' / 'state' / 'latest.json').read_text())
        return r, latest

    def test_repair_probe_runs_the_real_adapter_protocol_and_publishes_nothing(self):
        claude = self.make_claude(cli_ok({'status': 'resolved', 'reason': 'both kept',
                                          'edits': [{'path': 'agent/curator.py', 'content': RESOLVED_PROBE}]}))
        r, latest = self.probe(str(claude.bin))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('repair probe passed', r.stdout)
        self.assertEqual(latest['status'], 'prepared')
        self.assertEqual(latest['publish']['status'], 'skipped-prepare-only')
        self.assertEqual(latest['repairs']['attempts'], 1)
        self.assertEqual([m['result'] for m in latest['merges']], ['merged+repaired'])
        self.assertNotIn('SLOPTIMUS-PRIME', json.dumps(latest['input']))
        seen = claude.seen()
        assert seen is not None
        payload = json.loads(seen['stdin'])
        self.assertEqual([c['path'] for c in payload['conflicts']], ['agent/curator.py'])
        self.assertIn('<<<<<<<', payload['conflicts'][0]['merged'])
        self.assertEqual(seen['env']['CLAUDE_CODE_OAUTH_TOKEN'], OAUTH)
        self.assertFalse([k for k in seen['env'] if k.startswith(('GITHUB', 'GIT_CONFIG'))])
        self.assertNotIn(OAUTH, r.stdout + r.stderr + json.dumps(latest))
        self.assertNotIn(GH, r.stdout + r.stderr + json.dumps(latest))

    def test_repair_probe_fails_when_claude_loses_either_side_of_the_conflict(self):
        wrong = RESOLVED_PROBE.replace('= 60', '= 57')
        claude = self.make_claude(cli_ok({'status': 'resolved', 'reason': 'dropped upstream',
                                          'edits': [{'path': 'agent/curator.py', 'content': wrong}]}))
        r, latest = self.probe(str(claude.bin))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotEqual(latest['status'], 'prepared')

    def test_repair_probe_rejects_edits_outside_the_allowlist(self):
        claude = self.make_claude(cli_ok({'status': 'resolved', 'reason': 'sneaky',
                                          'edits': [{'path': '.github/workflows/fork-maintenance.yml', 'content': 'on: push\n'}]}))
        r, latest = self.probe(str(claude.bin))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('not allowlisted', json.dumps(latest['repairs']))

    def test_probe_cli_failure_is_reported(self):
        claude = self.make_claude(json.dumps({'is_error': True, 'result': 'Not logged in'}), rc=1)
        r, latest = self.probe(str(claude.bin))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(latest['status'], 'blocked')
        self.assertIn('Not logged in', latest['reason'])


if __name__ == '__main__':
    unittest.main()
