"""Parent-only real SQLite/owner receipt fixtures. No models, network or dispatch.

Run directly with unittest in the parent's clean environment. These tests never
unset/patch the delegated-child fence; under delegation every case is skipped.
The agent's inference and the owner's scheduler are deliberately not executed.
"""
import asyncio
import contextlib
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch

from agent.delegation_context import is_delegated_child_process_context
from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_execution as admission


@unittest.skipIf(is_delegated_child_process_context(), 'parent-only native fixtures; delegation fence preserved')
class NativeReviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        if is_delegated_child_process_context():
            self.skipTest('parent-only native fixtures; delegation fence preserved')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='kanban-binding-review-', dir=os.environ['TMPDIR'])))
        self.stack.enter_context(patch.dict(os.environ, {
            'HERMES_HOME': str(self.root), 'HERMES_KANBAN_HOME': str(self.root),
            'HERMES_KANBAN_BOARD': 'default', 'HERMES_KANBAN_DB': '',
            'HERMES_KANBAN_TASK': '', 'HERMES_KANBAN_RUN_ID': '', 'HERMES_KANBAN_CLAIM_LOCK': '',
            'HERMES_KANBAN_WORKSPACES_ROOT': '', 'HERMES_PROFILE': 'default'}))
        self.stack.enter_context(patch.object(kb, '_fire_kanban_lifecycle_hook'))
        self.conn = self.stack.enter_context(kbc.connect_closing())
        self.path = Path(admission._db_identity(self.conn))
        self.assertEqual(self.path, self.root / 'kanban.db')
        self.config = {'kanban': {'execution_admission': 'required'},
            'model': {'default': 'fixture-model', 'provider': 'custom'},
            'platform_toolsets': {'cli': ['kanban']}}
        (self.root / 'config.yaml').write_text(json.dumps(self.config))
        self.addCleanup(admission.bind_worker, None)

    def claim(self, *, sealed=True):
        if not sealed:
            self.config['kanban']['execution_admission'] = 'off'
            (self.root / 'config.yaml').write_text(json.dumps(self.config))
        tid = kb.create_task(self.conn, title='Synthetic bound review', body='APPROVED', assignee='default',
                             workspace_kind='dir', workspace_path=str(self.root), project_id='')
        spec = admission.capture_execution_spec(self.conn, tid, board='default') if sealed else None
        task = kb.claim_task(self.conn, tid, expected_spec=spec)
        assert task is not None
        binding = admission.read_binding(self.conn, tid, task.current_run_id, task.claim_lock)
        params = dict(board='default', task_id=tid, run_id=task.current_run_id, claim_lock=task.claim_lock,
                      db=str(self.path))
        return task, binding, params

    def test_foreign_runtime_cannot_adopt_same_default_profile_name(self):
        task, binding, params = self.claim()
        assert binding is not None
        self.assertEqual(binding['spec']['profile_id'], str(self.root))
        foreign = self.root / 'foreign'
        foreign.mkdir()
        connection = SimpleNamespace(native_owner=True, authority=SimpleNamespace(profile_id=str(foreign)),
            actor=SimpleNamespace(profile_id=str(foreign), capabilities={'session:create'}))
        with patch.dict(os.environ, {'HERMES_HOME': str(foreign)}):
            with self.assertRaisesRegex(ValueError, 'profile_mismatch'):
                session_kanban.build_kanban_policy(connection, params, self.config)
        live = kb.get_task(self.conn, task.id)
        assert live is not None
        self.assertEqual(live.status, 'running')

    def test_initial_turn_checks_real_adoption_and_later_terminal_claim(self):
        task, binding, params = self.claim()
        assert binding is not None
        context: dict[str, Any] = dict(params, execution_binding=binding, profile='default', workspace=str(self.root),
            workspaces_root=str(self.root), branch=None, tenant=None, max_runtime_seconds=None,
            skills=[], context=binding['spec']['context'], goal_mode=False, goal_text='APPROVED', goal_max_turns=1)
        frame = {'home': str(self.root), 'text': 'work', 'policy': {
            'source': 'kanban', 'config_json': '{}', 'kanban_json': json.dumps(context)}}
        with patch.object(session_kanban, '_watch_submitter'), patch('os.chdir'):
            session_kanban.bind_worker_context(frame)
        agent = Mock(session_id='synthetic-only')
        agent.run_conversation.return_value = {'completed': True, 'final_response': 'synthetic-only'}
        session_kanban._run_task_turns(agent, frame, [], context)
        agent.run_conversation.assert_called_once()
        agent.reset_mock()
        forged = copy.deepcopy(context)
        forged['execution_binding']['spec']['context'] = 'UNVALIDATED'
        forged['execution_binding']['digest'] = admission.fingerprint(forged['execution_binding']['spec'])
        with self.assertRaisesRegex(ValueError, 'invalid_execution_binding'):
            session_kanban._run_task_turns(agent, frame, [], forged)
        kb.complete_task(self.conn, task.id, result='fixture completed')
        live = kb.get_task(self.conn, task.id)
        assert live is not None
        self.assertIsNone(live.current_run_id)
        self.assertEqual(admission.read_run_binding(self.conn, task.id, params['run_id']), binding)
        with self.assertRaisesRegex(ValueError, 'stale_kanban_claim'):
            session_kanban._run_task_turns(agent, frame, [], context)
        agent.run_conversation.assert_not_called()

    def test_terminal_unsealed_worker_show_keeps_default_off_behavior(self):
        task, binding, params = self.claim(sealed=False)
        self.assertIsNone(binding)
        kb.complete_task(self.conn, task.id, result='fixture completed')
        with patch.dict(os.environ, {'HERMES_KANBAN_TASK': task.id,
                'HERMES_KANBAN_RUN_ID': str(params['run_id']), 'HERMES_KANBAN_CLAIM_LOCK': params['claim_lock']}):
            self.assertIsNone(admission.worker_show(self.conn, task.id))

    def test_terminal_receipt_replays_real_owner_admission_without_submit(self):
        from gateway.session_controls import AuthorityConnection
        from gateway.session_contract import SessionRef
        from hermes_state_runtime import list_session_admissions
        from tests.gateway.test_session_follow_profile_config import _owner
        task, binding, params = self.claim()
        store, runner, authority, dashboard = _owner(self.root)
        authority.profile_id = str(self.root)
        self.stack.enter_context(patch.object(authority, '_schedule', return_value=None))
        self.stack.enter_context(patch('gateway.run._load_gateway_config', return_value=self.config))
        connection = AuthorityConnection(authority, object(), {
            'user_id': 'owner', 'provider': 'local', 'profile_id': str(self.root),
            'instance_id': authority.instance_id, 'native_bootstrap': True,
            'capabilities': ['session:create', 'session:read', 'session:submit', 'session:control']})

        async def scenario():
            try:
                first = await session_kanban.run_task(connection, params)
                ref = SessionRef(str(self.root), first['session_id'])
                # Settle storage without running a model: cancellation is an
                # honest terminal receipt, not fabricated model completion.
                terminal = await authority.cancel_queued(connection.actor, ref, first['receipt']['admission_id'])
                self.assertEqual(terminal.status, 'terminal')
                kb.complete_task(self.conn, task.id, result='synthetic native completion')
                live = kb.get_task(self.conn, task.id)
                assert live is not None
                self.assertIsNone(live.current_run_id)
                with patch.object(authority, 'submit', new_callable=AsyncMock) as submit, \
                     patch.object(connection, 'resume', new_callable=AsyncMock) as resume:
                    replay = await session_kanban.run_task(connection, params)
                    self.assertEqual(replay['receipt'], asdict(terminal))
                    submit.assert_not_awaited()
                    resume.assert_not_awaited()
                self.assertEqual(len(list_session_admissions(authority.db, session_id=ref.session_id,
                                                            pending_only=False)), 1)
                self.assertEqual(admission.read_run_binding(self.conn, task.id, params['run_id']), binding)
            finally:
                await connection.close()
                await dashboard.close()
                store.close_all_db_handles()
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
