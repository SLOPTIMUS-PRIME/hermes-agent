"""Admission contract tests; fake seams never disable the delegation fence.

Run directly with unittest under delegation: the repository's hermetic pytest
runner drops HERMES_DELEGATED_CHILD_CONTEXT. Native fixtures below are parent-only.
"""
import contextlib
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch
from typing import Any

from hermes_cli import kanban_db as kb


def task(**changes):
    values: dict[str, Any] = dict(id='t_sealed', title='Validated', body='Read synthetic fixture',
                  assignee='test-worker', status='ready', priority=1, created_by='owner',
                  created_at=100, started_at=None, completed_at=None,
                  workspace_kind='dir', workspace_path='/fixture', claim_lock=None,
                  claim_expires=None, tenant='synthetic', skills=[])
    values.update(changes)
    return kb.Task(**values)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('hermes_cli.profiles.resolve_profile_env', return_value='/fixture/profile'))
        stack.enter_context(patch.object(kb, 'list_attachments', return_value=[]))

    def test_required_claim_rejects_without_binding_before_cas(self):
        # A fake transaction/connection is deliberate: no native DB or board is
        # opened, and no delegation check is patched for a real operation.
        conn = Mock()
        with patch.object(kb, 'write_txn', return_value=contextlib.nullcontext()), \
             patch.object(kb, '_parents_satisfied', return_value=True), \
             patch.object(kb, '_reclaim_dangling_run'), \
             patch.object(kb, '_claim_and_open_run', return_value=7) as cas, \
             patch.object(kb, 'get_task', return_value=task()), \
             patch.object(kb, '_fire_task_hook'), \
             patch('pathlib.Path.read_text', return_value=
                   'kanban:\n  execution_admission: required\n'):
            with self.assertRaisesRegex(ValueError, 'execution_admission_required'):
                kb.claim_task(conn, 't_sealed')
        cas.assert_not_called()

    def test_context_renderer_can_freeze_task_and_clock(self):
        from hermes_cli import kanban_db_context as ctx
        frozen = task(status='running')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(kb, 'get_task', side_effect=AssertionError('mutable task read')))
            stack.enter_context(patch.object(kb, 'list_attachments', return_value=[]))
            stack.enter_context(patch.object(kb, 'list_comments', return_value=[]))
            for name in ('_ctx_prior_attempts', '_ctx_parent_results', '_ctx_role_history'):
                stack.enter_context(patch.object(ctx, name))
            rendered = kb.build_worker_context(Mock(), frozen.id, now=100, task=frozen)
        self.assertIn(frozen.body, rendered)
        self.assertIn('Status:   running', rendered)

    def test_new_foreign_high_priority_row_is_not_dispatched(self):
        from hermes_cli import kanban_db_dispatch as dispatch
        admitted = {'t_sealed': {'sentinel': 'private expected spec'}}
        result_rows = [{'id': 't_foreign', 'assignee': 'test-worker'},
                       {'id': 't_sealed', 'assignee': 'test-worker'}]
        with patch.object(dispatch, '_run_reclaim_phase'), \
             patch.object(dispatch, '_tick_spawn_budget', return_value=(True, 1)), \
             patch.object(dispatch, '_lane_rows', return_value=result_rows), \
             patch.object(dispatch, 'review_dispatch_enabled', return_value=False), \
             patch.object(dispatch, '_resolve_default_assignee', return_value=None), \
             patch.object(dispatch, '_dispatch_lane_task', return_value=True) as spawn:
            dispatch._dispatch_once_locked(Mock(), expected_specs=admitted)
        self.assertEqual([c.args[1]['id'] for c in spawn.call_args_list], ['t_sealed'])
        self.assertEqual(spawn.call_args.kwargs['expected_spec'], admitted['t_sealed'])

    def test_completion_and_edit_reject_reserved_metadata(self):
        from hermes_cli.kanban_execution import BINDING_KEY
        for method in (kb.complete_task, kb.edit_task, kb.request_review, kb._synthesize_ended_run):
            with self.subTest(method=method.__name__):
                conn = Mock()
                kwargs: dict[str, Any] = {'metadata': {BINDING_KEY: {'spec': 'forged'}}}
                if method is kb._synthesize_ended_run:
                    kwargs['outcome'] = 'completed'
                with self.assertRaisesRegex(ValueError, 'reserved_execution_metadata'):
                    method(conn, 't_sealed', **kwargs)
                conn.execute.assert_not_called()

    def test_closing_run_preserves_seal_beside_worker_metadata(self):
        from hermes_cli.kanban_execution import BINDING_KEY
        conn = Mock()
        original = {BINDING_KEY: {'digest': 'fixture-only'}}
        conn.execute.return_value.fetchone.return_value = (json.dumps(original),)
        with patch.object(kb, '_current_run_id', return_value=7):
            kb._end_run(conn, 't_sealed', outcome='completed', metadata={'worker_session_id': 'sid'})
        update = next(c for c in conn.execute.call_args_list if 'UPDATE task_runs' in c.args[0])
        saved = json.loads(update.args[1][4])
        self.assertEqual(saved[BINDING_KEY], original[BINDING_KEY])
        self.assertEqual(saved['worker_session_id'], 'sid')

    def test_null_expected_entry_never_degrades_to_legacy_dispatch(self):
        from hermes_cli import kanban_db_dispatch as dispatch
        with patch.object(dispatch, '_run_reclaim_phase') as reclaim, \
             patch.object(dispatch, '_tick_spawn_budget', return_value=(False, 0)), \
             patch('hermes_cli.kanban_execution.required', return_value=False):
            with self.assertRaisesRegex(ValueError, 'invalid_execution_specs'):
                dispatch._dispatch_once_locked(Mock(), expected_specs={'t_sealed': None})
        reclaim.assert_not_called()

    def test_low_level_claim_cannot_open_unbound_run_in_required_mode(self):
        conn = Mock()
        with patch('hermes_cli.kanban_execution.required', return_value=True):
            with self.assertRaisesRegex(ValueError, 'execution_admission_required'):
                kb._claim_and_open_run(conn, 't_sealed', 'ready', 'claim', 100, 10)
        conn.execute.assert_not_called()

    def test_cli_expected_specs_are_private_input_to_native_dispatch(self):
        import argparse
        import io
        from hermes_cli import kanban_ops, kanban_db_dispatch
        expected = {'t_sealed': {'private': 'validated'}}
        args = argparse.Namespace(dry_run=False, max=1, json=True, expected_specs='-')
        with patch('sys.stdin', io.StringIO(json.dumps(expected))), \
             patch.object(kanban_ops.kbc, 'connect_closing', return_value=contextlib.nullcontext(Mock())), \
             patch.object(kanban_ops.kbd, 'dispatch_once', return_value=kanban_db_dispatch.DispatchResult()) as dispatch, \
             patch('hermes_cli.config.load_config', return_value={}), \
             patch.object(kanban_ops.kbd, 'resolve_max_in_progress', return_value=1), \
             patch.object(kanban_ops, '_print_json'):
            self.assertEqual(kanban_ops._cmd_dispatch(args), 0)
        self.assertEqual(dispatch.call_args.kwargs.get('expected_specs'), expected)

    def test_required_config_follows_database_not_foreign_profile_home(self):
        from pathlib import Path
        from hermes_cli import kanban_execution as admission
        def config(path, *args, **kwargs):
            if path == Path('/fixture/root/config.yaml'):
                return 'kanban:\n  execution_admission: required\n'
            raise FileNotFoundError(path)
        with patch.object(kb, 'kanban_home', return_value=Path('/fixture/foreign')), \
             patch('hermes_cli.kanban_db_connect._main_db_file',
                   return_value='/fixture/root/kanban/boards/test/kanban.db'), \
             patch.object(Path, 'read_text', config):
            self.assertTrue(admission.required(Mock()))

    def test_preclaim_mutation_is_rejected_at_native_cas(self):
        from hermes_cli import kanban_execution as admission
        from dataclasses import replace
        original = task()
        conn = Mock(in_transaction=True)
        with patch.object(kb, 'get_task', return_value=original), \
             patch.object(kb, 'build_worker_context', return_value='captured context'), \
             patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'):
            spec = admission.capture_execution_spec(conn, original.id, board='test', captured_at=100)
        mutations = ({'body': 'changed'}, {'title': 'changed'}, {'skills': ['evil']},
                     {'model_override': 'evil'}, {'provider_override': 'evil'},
                     {'reasoning_effort': 'high'}, {'assignee': 'other'},
                     {'workspace_path': '/other'}, {'goal_mode': True})
        for mutation in mutations:
            with self.subTest(mutation=mutation), \
                 patch.object(kb, 'get_task', return_value=replace(original, **mutation)), \
                 patch.object(kb, 'build_worker_context', return_value='captured context'), \
                 patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'):
                conn.reset_mock()
                with self.assertRaises(ValueError):
                    kb._claim_and_open_run(conn, original.id, 'ready', 'claim', 200, 100, expected_spec=spec)
                conn.execute.assert_not_called()
        with patch.object(kb, 'get_task', return_value=original), \
             patch.object(kb, 'build_worker_context', return_value='changed parent/comment context'), \
             patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'):
            with self.assertRaisesRegex(ValueError, 'execution_spec_mismatch'):
                kb._claim_and_open_run(conn, original.id, 'ready', 'claim', 200, 100, expected_spec=spec)
        conn.execute.assert_not_called()

    def test_valid_claim_persists_spec_inside_native_transaction(self):
        from hermes_cli import kanban_execution as admission
        original = task()
        conn = Mock(in_transaction=True)
        conn.execute.return_value.rowcount = 1
        conn.execute.return_value.lastrowid = 7
        conn.execute.return_value.fetchone.return_value = {
            'assignee': original.assignee, 'max_runtime_seconds': None, 'current_step_key': None}
        with patch.object(kb, 'get_task', return_value=original), \
             patch.object(kb, 'build_worker_context', return_value='captured context'), \
             patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'), \
             patch.object(kb, '_append_event'):
            spec = admission.capture_execution_spec(conn, original.id, board='test', captured_at=100)
            run_id = kb._claim_and_open_run(conn, original.id, 'ready', 'claim', 200, 100, expected_spec=spec)
        self.assertEqual(run_id, 7)
        seal = next(c for c in conn.execute.call_args_list if 'SET metadata=' in c.args[0])
        binding = json.loads(seal.args[1][0])[admission.BINDING_KEY]
        self.assertEqual(binding['spec'], spec)
        self.assertEqual((binding['task_id'], binding['run_id'], binding['claim_lock']), (original.id, 7, 'claim'))
        self.assertEqual(binding['digest'], admission.fingerprint(spec))

    def test_legacy_unbound_claim_is_unchanged_when_off(self):
        conn = Mock()
        with patch.object(kb, 'write_txn', return_value=contextlib.nullcontext()), \
             patch.object(kb, '_parents_satisfied', return_value=True), \
             patch.object(kb, '_reclaim_dangling_run'), \
             patch.object(kb, '_claim_and_open_run', return_value=7) as cas, \
             patch.object(kb, 'get_task', return_value=task()), \
             patch.object(kb, '_fire_task_hook'), \
             patch('hermes_cli.kanban_execution.required', return_value=False):
            self.assertEqual(kb.claim_task(conn, 't_sealed').id, 't_sealed')
        cas.assert_called_once()


if __name__ == '__main__':
    unittest.main()
