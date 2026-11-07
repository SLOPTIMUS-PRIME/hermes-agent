"""Static guarantees for .github/workflows/fork-maintenance.yml (stdlib only: no YAML parser on CI)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
WORKFLOW = REPO / '.github' / 'workflows' / 'fork-maintenance.yml'
PINS = {
    'actions/checkout': '11d5960a326750d5838078e36cf38b85af677262',
    'actions/setup-python': 'a26af69be951a213d495a4c3e4e4022e16d87065',
    'actions/upload-artifact': 'ea165f8d65b6e75b540449e92b4886f43607fa02',
}


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not WORKFLOW.exists():
            raise unittest.SkipTest('workflow is not part of this checkout')
        cls.text = WORKFLOW.read_text()

    def test_only_scheduled_and_manual_triggers(self):
        on = self.text.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual(re.findall(r'^  ([a-z_]+):', on, re.M), ['schedule', 'workflow_dispatch'])
        self.assertIn("- cron: '15 14 * * *'", self.text)
        self.assertEqual(re.findall(r'^      ([a-z_]+):\s*$', on, re.M), ['repair_probe'])
        self.assertRegex(on, r'repair_probe:\n(?:        .*\n)*?        type: boolean')
        self.assertNotIn('pull_request', self.text)
        self.assertNotRegex(self.text, r'(?m)^\s+push:')

    def test_least_privilege_and_main_only(self):
        self.assertRegex(self.text, r'(?m)^permissions: \{\}$')
        self.assertEqual(re.findall(r'(?m)^\s+contents: (\w+)$', self.text), ['write'])
        self.assertNotRegex(self.text, r'(?m)^\s+(id-token|actions|packages|pull-requests|workflows|secrets):')
        self.assertIn("github.ref == 'refs/heads/main'", self.text)
        self.assertIn("github.repository == 'SLOPTIMUS-PRIME/hermes-agent'", self.text)
        self.assertIn('cancel-in-progress: false', self.text)
        self.assertNotIn('pull_request_target', self.text)

    def test_actions_are_the_requested_immutable_pins(self):
        uses = re.findall(r'(?m)^\s+uses: (\S+)', self.text)
        self.assertTrue(uses)
        for ref in uses:
            name, sha = ref.split('@')
            self.assertEqual(PINS[name], sha, ref)
        self.assertEqual(set(u.split('@')[0] for u in uses), set(PINS))
        self.assertNotIn('claude-code-action', self.text)

    def test_checkouts_never_persist_credentials(self):
        blocks = re.findall(r'uses: actions/checkout@\S+\n        with:\n((?:          .*\n)+)', self.text)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertIn('persist-credentials: false', block)
        self.assertIn('path: control', blocks[0])
        self.assertIn('ref: ${{ github.sha }}', blocks[0])
        self.assertIn('ref: nick-patches', blocks[1])
        self.assertIn('path: seed', blocks[1])

    def test_no_user_controlled_expression_reaches_a_shell(self):
        for run in re.findall(r'(?ms)^        run: (?:\|\n(.*?)(?=^      - |\Z)|(.*?)$)', self.text):
            body = ''.join(run)
            self.assertNotIn('${{', body)
        # the one dispatch input is only consumed through an env var
        self.assertEqual(len(re.findall(r'inputs\.', self.text)), 2)
        self.assertNotIn('github.event.inputs', self.text)

    def test_secrets_are_scoped_to_the_single_maintenance_step(self):
        steps = re.split(r'(?m)^      - name: ', self.text)[1:]
        holders = [s.splitlines()[0] for s in steps if 'secrets.' in s or 'github.token' in s]
        self.assertEqual(holders, ['Maintain the fork'])
        self.assertEqual(len(re.findall(r'secrets\.', self.text)), 1)
        self.assertIn('secrets.CLAUDE_CODE_OAUTH_TOKEN', self.text)
        self.assertNotIn('ANTHROPIC_API_KEY', self.text)
        self.assertNotIn('--bare', self.text)

    def test_artifact_is_exactly_latest_json_and_always_uploaded(self):
        block = self.text.split('- name: Upload maintenance result', 1)[1]
        self.assertIn('if: always()', block)
        self.assertIn('name: fork-maintenance-result', block)
        self.assertIn('path: ${{ runner.temp }}/fork-maintenance/state/latest.json', block)
        self.assertEqual(self.text.count('upload-artifact@'), 1)
        self.assertRegex(self.text, r'(?s)Ensure a result exists\n        if: always\(\)')

    def test_pinned_claude_cli_and_locked_installs(self):
        lock = (ROOT / 'claude-cli' / 'package-lock.json').read_text()
        self.assertIn('"@anthropic-ai/claude-code": "2.1.319"', (ROOT / 'claude-cli' / 'package.json').read_text())
        self.assertIn('npm ci --ignore-scripts', self.text)
        self.assertIn('2.1.319 (Claude Code)', self.text)
        self.assertIn('"version": "2.1.319"', lock)
        self.assertGreaterEqual(lock.count('"integrity"'), 2)
        self.assertIn('--frozen', self.text)
        self.assertIn('--require-hashes', self.text)
        self.assertTrue(all('--hash=sha256:' in b for b in (ROOT / 'test-requirements.txt').read_text().split('\n\n') if '==' in b))


if __name__ == '__main__':
    unittest.main()
