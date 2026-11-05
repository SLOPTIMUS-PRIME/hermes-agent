"""Review regressions at fake I/O seams; never weaken the delegation fence."""
import contextlib
import asyncio
import copy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gateway import session_kanban
from gateway.session_policy import LocalSessionPolicy
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_execution as admission
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_kanban_execution_binding import sealed_fixture
from tests.hermes_cli.test_kanban_execution_admission import task


class ReviewBoundaryTests(unittest.TestCase):
    def tearDown(self):
        admission.bind_worker(None)

    def policy_seam(self, stack, profile='/fixture/profile'):
        conn, live, run, binding = sealed_fixture()
        conn.execute.return_value.fetchone.side_effect = [
            dict(status='running', task_id=live.id, claim_lock='claim-7'), None]
        connection = SimpleNamespace(native_owner=True,
            authority=SimpleNamespace(profile_id=profile),
            actor=SimpleNamespace(profile_id=profile, capabilities={'session:create'}))
        policy = LocalSessionPolicy('cli', 'cli', '/fixture', 'approved-model', (), '{}', '{}', '{}')
        stack.enter_context(patch.object(session_kanban.sqlite3, 'connect', return_value=conn))
        stack.enter_context(patch.object(kb, 'kanban_db_path', return_value=Path('/fixture/kanban.db')))
        stack.enter_context(patch.object(kb, 'get_task', return_value=live))
        stack.enter_context(patch.object(kb, 'get_run', return_value=run))
        stack.enter_context(patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'))
        stack.enter_context(patch('hermes_cli.profiles.resolve_profile_env', return_value=profile))
        build = stack.enter_context(patch('gateway.session_policy.build_policy', return_value=policy))
        params = dict(board='test', task_id=live.id, run_id=7, claim_lock='claim-7')
        return connection, params, binding, build

    def test_1_foreign_runtime_same_name_cannot_adopt_seal(self):
        with contextlib.ExitStack() as stack:
            connection, params, binding, build = self.policy_seam(stack, '/foreign/profiles/test-worker')
            with self.assertRaisesRegex(RuntimeStoreError, 'profile_mismatch'):
                session_kanban.build_kanban_policy(connection, params, {})
        build.assert_not_called()
        self.assertEqual(binding['spec']['profile_id'], '/fixture/profile')

    def inference_seam(self, stack):
        conn, live, run, binding = sealed_fixture()
        admission.bind_worker(binding)
        stack.enter_context(patch.object(session_kanban.sqlite3, 'connect', return_value=conn))
        stack.enter_context(patch.object(kb, 'get_task', return_value=live))
        stack.enter_context(patch.object(kb, 'get_run', return_value=run))
        stack.enter_context(patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'))
        current = stack.enter_context(patch.object(session_kanban, '_bound_claim_is_live', return_value=True))
        stack.enter_context(patch('agent.kanban_turn_recovery.recover_failed_kanban_turns'))
        agent = Mock(session_id='fake-only')
        agent.run_conversation.return_value = {'completed': True, 'final_response': 'fixture'}
        context: dict[str, Any] = dict(task_id=live.id, run_id=7, claim_lock='claim-7', db='/fixture/kanban.db',
                       skills=['approved'], context='APPROVED CONTEXT', goal_mode=False,
                       execution_binding=copy.deepcopy(binding))
        return agent, context, live, run, binding, current

    def test_2_initial_inference_rechecks_claim_and_adopted_capsule(self):
        for mutation in ('terminal', 'expired', 'replacement', 'unbound'):
            with self.subTest(mutation=mutation), contextlib.ExitStack() as stack:
                agent, context, live, run, binding, current = self.inference_seam(stack)
                if mutation == 'terminal':
                    live.status = 'done'
                    run.status = 'completed'
                elif mutation == 'expired':
                    current.return_value = False
                elif mutation == 'replacement':
                    context['execution_binding']['spec']['context'] = 'FORGED CAPSULE'
                    context['execution_binding']['digest'] = admission.fingerprint(context['execution_binding']['spec'])
                else:
                    admission.bind_worker(None)
                with self.assertRaises(ValueError):
                    session_kanban._run_task_turns(agent, {'text': 'run'}, [], context)
                agent.run_conversation.assert_not_called()

    def test_initial_inference_accepts_exact_adopted_live_capsule(self):
        with contextlib.ExitStack() as stack:
            agent, context, *_ = self.inference_seam(stack)
            session_kanban._run_task_turns(agent, {'text': 'UNTRUSTED'}, [], context)
        self.assertIn('APPROVED CONTEXT', agent.run_conversation.call_args.args[0])
        self.assertNotIn('UNTRUSTED', agent.run_conversation.call_args.args[0])

    def test_5_bound_config_cannot_reinject_live_task_via_prompt_hook(self):
        from agent import shell_hooks
        from agent.turn_context import _collect_pre_llm_call_context
        from gateway.session_policy import register_worker_hooks
        from hermes_cli import plugins
        config = {'hooks_auto_accept': True, 'hooks': {
            'pre_llm_call': [{'command': 'synthetic-task-read'}],
            'pre_tool_call': [{'command': 'synthetic-tool-policy'}]}}
        for sealed in (True, False):
            with self.subTest(sealed=sealed), contextlib.ExitStack() as stack:
                connection, params, _, build = self.policy_seam(stack)
                if not sealed:
                    stack.enter_context(patch.object(admission, 'read_binding', return_value=None))
                    stack.enter_context(patch.object(kb, 'build_worker_context', return_value='legacy context'))
                    stack.enter_context(patch.object(kb, '_retry_status_for_run', return_value='ready'))
                base = build.return_value
                build.side_effect = lambda params, cfg, **kw: replace(base, config_json=json.dumps(cfg))
                policy, _ = session_kanban.build_kanban_policy(connection, params, config)
                # Run actual registration, response parsing and turn-context injection;
                # only the subprocess and plugin discovery/consent I/O are fakes.
                manager = plugins.PluginManager()
                stack.enter_context(patch.object(plugins, 'get_plugin_manager', return_value=manager))
                stack.enter_context(patch.object(plugins, '_delivery_manager', return_value=manager))
                stack.enter_context(patch.object(shell_hooks, '_registered', set()))
                stack.enter_context(patch.object(shell_hooks, 'allowlisted_pairs', return_value={
                    ('pre_llm_call', 'synthetic-task-read'), ('pre_tool_call', 'synthetic-tool-policy')}))
                spawn = stack.enter_context(patch.object(shell_hooks, '_spawn', return_value={
                    'error': None, 'timed_out': False, 'stderr': '', 'returncode': 0,
                    'stdout': json.dumps({'context': 'MUTATED LIVE TASK'})}))
                stack.enter_context(patch('hermes_cli.lifecycle._observe'))
                stack.enter_context(patch('tools.hook_output_spill.get_spill_config', return_value={}))
                register_worker_hooks(policy)
                agent = SimpleNamespace(_persist_disabled=False, session_id='fake-only', model='fake')
                injected = _collect_pre_llm_call_context(agent, effective_task_id='t_sealed', turn_id='turn',
                    original_user_message='APPROVED CONTEXT', messages=[], conversation_history=[])
                self.assertEqual(injected, '' if sealed else 'MUTATED LIVE TASK')
                self.assertEqual(spawn.call_count, 0 if sealed else 1)
                self.assertTrue(manager.has_hook('pre_tool_call'))
        self.assertIn('pre_llm_call', config['hooks'], 'do not mutate caller-owned configuration')

    def receipt_seam(self, stack, *, sealed=True):
        from gateway.session_contract import AdmissionReceipt, SessionRef
        from hermes_cli import kanban_db_connect as kbc
        conn, live, run, binding = sealed_fixture()
        live.status, live.current_run_id, live.claim_lock = 'done', None, None
        run.status = 'completed'
        if not sealed:
            run.metadata = {}
        params = dict(board='test', task_id=live.id, run_id=7, claim_lock='claim-7')
        context: dict[str, Any] = dict(params, db='/fixture/kanban.db')
        if sealed:
            context['execution_binding'] = binding
        policy = LocalSessionPolicy('kanban', 'cli', '/fixture', 'approved-model', (), '{}', '{}', '{}',
                                    kanban_json=json.dumps(context))
        ref = SessionRef('/fixture/profile', 'saved-session')
        receipt = AdmissionReceipt('saved-admission', ref, 1, 'terminal', 'completed', 1, 1)
        authority = SimpleNamespace(profile_id=ref.profile_id, db=Mock(db_path='/fixture/state.db'),
                                    submit=AsyncMock(return_value=receipt), receipt=AsyncMock(return_value=receipt))
        actor = SimpleNamespace(profile_id=ref.profile_id, subject='owner', capabilities={'session:create', 'session:submit'})
        connection = SimpleNamespace(native_owner=True, authority=authority, actor=actor, resume=AsyncMock())
        marker = {'db': '/fixture/state.db', 'session_id': ref.session_id,
                  'request_id': 'kanban:' + json.dumps(['test', live.id, 7], separators=(',', ':'))}
        conn.execute.return_value.fetchone.side_effect = lambda: (json.dumps(marker),)
        stack.enter_context(patch('hermes_state_local.local_receipt', return_value={'policy': asdict(policy)}))
        stack.enter_context(patch('gateway.session_policy.restore_policy', return_value=policy))
        stack.enter_context(patch('gateway.session_local_recovery.local_identity', return_value=ref.session_id))
        stack.enter_context(patch('gateway.session_local_recovery.restore_local_session', return_value=ref))
        stack.enter_context(patch.object(kbc, 'connect_closing', return_value=contextlib.nullcontext(conn)))
        stack.enter_context(patch.object(kb, 'write_txn', return_value=contextlib.nullcontext()))
        stack.enter_context(patch.object(kb, 'get_task', return_value=live))
        stack.enter_context(patch.object(kb, 'get_run', return_value=run))
        stack.enter_context(patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'))
        stack.enter_context(patch.object(admission, 'required', return_value=False))
        async def tracked(authority, fn):
            return fn()
        stack.enter_context(patch('gateway.session_runtime_workers.tracked_write', side_effect=tracked))
        committed = stack.enter_context(patch('hermes_state_runtime.find_session_admission', return_value={
            'admission_id': receipt.admission_id, 'status': 'terminal',
            'payload': {'text': f'work kanban task {live.id}', 'finite': True}}))
        return connection, params, marker, run, committed, receipt

    def test_7_terminal_sealed_retry_returns_only_committed_receipt(self):
        with contextlib.ExitStack() as stack:
            connection, params, _, _, _, receipt = self.receipt_seam(stack)
            result = asyncio.run(session_kanban.run_task(connection, params))
        self.assertEqual(result, {'session_id': receipt.ref.session_id, 'receipt': asdict(receipt)})
        connection.authority.submit.assert_not_awaited()
        connection.resume.assert_not_awaited()
        connection.authority.receipt.assert_awaited_once()

    def test_terminal_retry_refuses_wrong_marker_missing_receipt_or_changed_seal(self):
        for mutation in ('db', 'session_id', 'request_id', 'no-receipt', 'seal', 'claim', 'profile'):
            with self.subTest(mutation=mutation), contextlib.ExitStack() as stack:
                connection, params, marker, run, committed, _ = self.receipt_seam(stack)
                if mutation in marker:
                    marker[mutation] = 'foreign'
                elif mutation == 'no-receipt':
                    committed.return_value = None
                elif mutation == 'seal':
                    run.metadata[admission.BINDING_KEY]['spec']['context'] = 'FORGED'
                    run.metadata[admission.BINDING_KEY]['digest'] = admission.fingerprint(
                        run.metadata[admission.BINDING_KEY]['spec'])
                elif mutation == 'claim':
                    run.claim_lock = 'different'
                elif mutation == 'profile':
                    connection.authority.profile_id = connection.actor.profile_id = '/foreign/profile'
                with self.assertRaises(ValueError):
                    asyncio.run(session_kanban.run_task(connection, params))
                connection.authority.submit.assert_not_awaited()
                connection.resume.assert_not_awaited()
                connection.authority.receipt.assert_not_awaited()

    def test_terminal_legacy_receipt_recovery_never_creates_new_admission(self):
        with contextlib.ExitStack() as stack:
            connection, params, _, _, _, receipt = self.receipt_seam(stack, sealed=False)
            result = asyncio.run(session_kanban.run_task(connection, params))
        self.assertEqual(result['receipt'], asdict(receipt))
        connection.authority.submit.assert_not_awaited()
        connection.resume.assert_not_awaited()

    def test_old_sealed_policy_with_prompt_hook_is_refused_before_adoption(self):
        _, _, _, binding = sealed_fixture()
        frame = {'policy': {'source': 'kanban', 'kanban_json': json.dumps({'execution_binding': binding}),
                           'config_json': json.dumps({'hooks': {'pre_llm_call': [{'command': 'live-card'}]}})}}
        with self.assertRaisesRegex(ValueError, 'execution_prompt_hooks_forbidden'):
            session_kanban.bind_worker_context(frame)
        self.assertIsNone(admission.worker_binding())

    def test_8_default_off_terminal_worker_show_keeps_legacy_behavior(self):
        conn, live, run, _ = sealed_fixture()
        live.status, live.current_run_id, live.claim_lock = 'done', None, None
        run.status, run.metadata = 'completed', {}
        with patch.dict(os.environ, {'HERMES_KANBAN_TASK': live.id, 'HERMES_KANBAN_RUN_ID': '7',
                                    'HERMES_KANBAN_CLAIM_LOCK': 'claim-7'}), \
             patch.object(kb, 'get_task', return_value=live), \
             patch.object(kb, 'get_run', return_value=run), \
             patch.object(admission, 'required', return_value=False):
            self.assertIsNone(admission.worker_show(conn, live.id))

    def test_attachment_snapshot_is_explicit_detached_and_mandatory(self):
        conn, live, run, binding = sealed_fixture()
        attachment = kb.Attachment(1, live.id, 'approved.txt', '/fixture/approved.txt', 'text/plain', 7, 'owner', 100)
        with patch.object(kb, 'get_task', return_value=task()), \
             patch.object(kb, 'list_attachments', return_value=[attachment]), \
             patch.object(kb, 'build_worker_context', return_value='APPROVED CONTEXT'), \
             patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'), \
             patch('hermes_cli.profiles.resolve_profile_env', return_value='/fixture/profile'):
            spec = admission.capture_execution_spec(conn, live.id, board='test', captured_at=100)
        self.assertEqual(spec['attachments'], [asdict(attachment)])
        attachment.filename = 'MUTATED'
        binding['spec'], binding['digest'] = spec, admission.fingerprint(spec)
        admission.bind_worker(binding)
        with patch.object(kb, 'get_task', return_value=live), \
             patch.object(kb, 'get_run', return_value=run), \
             patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'), \
             patch.object(kb, 'list_attachments', side_effect=AssertionError('live attachment read')):
            shown = admission.worker_show(conn, live.id)
            assert shown is not None
            self.assertEqual(shown['attachments'][0]['filename'], 'approved.txt')
            del binding['spec']['attachments']
            binding['digest'] = admission.fingerprint(binding['spec'])
            admission.bind_worker(binding)
            with self.assertRaisesRegex(ValueError, 'invalid_execution_binding'):
                admission.worker_show(conn, live.id)


if __name__ == '__main__':
    unittest.main()
