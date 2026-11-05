"""Parent-only native SQLite acceptance, never a gateway/model/live board.

Run with the parent's ordinary (non-delegated) environment:
  python -B -m unittest -v tests.hermes_cli.test_kanban_execution_native
The class SKIPS under delegation; it never clears or patches that fence.
"""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent.delegation_context import is_delegated_child_process_context
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_execution as admission


@unittest.skipIf(is_delegated_child_process_context(), 'native fixtures require non-delegated parent; fence preserved')
class NativeAdmissionTests(unittest.TestCase):
    def setUp(self):
        if is_delegated_child_process_context():
            self.skipTest('native fixtures require non-delegated parent; fence preserved')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='native-admission-fixture-')))
        self.stack.enter_context(patch.dict(os.environ, {'HERMES_HOME': str(self.root),
            'HERMES_KANBAN_HOME': str(self.root), 'HERMES_KANBAN_BOARD': 'default',
            'HERMES_KANBAN_DB': '', 'HERMES_KANBAN_TASK': '', 'HERMES_KANBAN_RUN_ID': '',
            'HERMES_KANBAN_CLAIM_LOCK': '', 'HERMES_KANBAN_WORKSPACES_ROOT': ''}))
        # Observers/profile discovery are not this fixture's execution authority.
        self.stack.enter_context(patch.object(kb, '_fire_kanban_lifecycle_hook'))
        self.stack.enter_context(patch.object(kb, '_fire_dispatch_tick_hook'))
        self.stack.enter_context(patch.object(dispatch, '_profile_exists_fn', return_value=lambda _: True))
        self.stack.enter_context(patch.object(dispatch, '_memory_pressure_level', return_value='unknown'))
        self.stack.enter_context(patch.object(dispatch, 'count_running_tasks_other_boards', return_value=0))
        self.stack.enter_context(patch.object(dispatch, 'review_dispatch_enabled', return_value=False))
        self.path = kb.kanban_db_path(board='default')
        self.assertEqual(self.path, self.root / 'kanban.db')
        self.conn = self.stack.enter_context(kbc.connect_closing(self.path))
        self.set_mode('required')
        profile = self.root / 'profiles' / 'fixture-worker'
        profile.mkdir(parents=True)
        (profile / 'config.yaml').write_text('{}\n', encoding='utf-8')
        self.addCleanup(admission.bind_worker, None)

    def set_mode(self, mode):
        (self.root / 'config.yaml').write_text('kanban:\n  execution_admission: "' + mode + '"\n', encoding='utf-8')

    def create(self, **changes):
        args = dict(title='Synthetic approved work', body='APPROVED BODY', assignee='fixture-worker',
                    workspace_kind='scratch', skills=[], project_id='')
        args.update(changes)
        return kb.create_task(self.conn, **args)

    def capture(self, tid, **kwargs):
        return admission.capture_execution_spec(self.conn, tid, board='default', **kwargs)

    def test_required_rejects_all_unbound_entrypoints(self):
        tid = self.create()
        for fn in (kb.claim_task, kb.claim_review_task):
            with self.assertRaisesRegex(ValueError, 'execution_admission_required'):
                fn(self.conn, tid)
        with self.assertRaisesRegex(ValueError, 'execution_admission_required'):
            dispatch.dispatch_once(self.conn, spawn_fn=lambda *_: self.fail('spawned'))
        self.assertEqual(kb.get_task(self.conn, tid).status, 'ready')
        self.assertEqual(kb.list_runs(self.conn, tid), [])

    def test_concurrent_insertion_cannot_expand_validated_set(self):
        tid = self.create()
        spec = self.capture(tid)
        with kbc.connect_closing(self.path) as other:
            foreign = kb.create_task(other, title='Foreign', body='NOT VALIDATED',
                assignee='fixture-worker', priority=999, workspace_kind='scratch', project_id='')
        spawned = []
        result = dispatch.dispatch_once(self.conn, expected_specs={tid: spec},
            spawn_fn=lambda task, workspace: spawned.append(task.id), max_spawn=1, max_in_progress=1)
        self.assertEqual(spawned, [tid])
        self.assertEqual([s[0] for s in result.spawned], [tid])
        self.assertEqual(kb.get_task(self.conn, foreign).status, 'ready')
        self.assertEqual(kb.list_runs(self.conn, foreign), [])

    def test_preclaim_body_mutation_has_no_run_or_spawn(self):
        tid = self.create()
        spec = self.capture(tid)
        with kbc.connect_closing(self.path) as other:
            kb.edit_task(other, tid, body='NOT VALIDATED')
        with self.assertRaisesRegex(ValueError, 'execution_spec_mismatch'):
            dispatch.dispatch_once(self.conn, expected_specs={tid: spec},
                spawn_fn=lambda *_: self.fail('spawned'), max_spawn=1, max_in_progress=1)
        self.assertEqual(kb.list_runs(self.conn, tid), [])
        self.assertEqual(kb.get_task(self.conn, tid).status, 'ready')

    def test_preclaim_comment_change_is_rejected(self):
        tid = self.create()
        spec = self.capture(tid)
        kb.add_comment(self.conn, tid, 'fixture', 'new instructions')
        with self.assertRaisesRegex(ValueError, 'execution_spec_mismatch'):
            kb.claim_task(self.conn, tid, expected_spec=spec)
        self.assertEqual(kb.list_runs(self.conn, tid), [])

    def test_claim_write_transaction_excludes_competing_writer(self):
        tid = self.create()
        spec = self.capture(tid)
        entered, attempted = threading.Event(), threading.Event()
        errors = []
        real_capture = admission.capture_execution_spec
        def writer():
            try:
                if not entered.wait(5):
                    raise AssertionError('claim did not reach snapshot')
                with contextlib.closing(sqlite3.connect(self.path, timeout=0, isolation_level=None)) as conn:
                    try:
                        conn.execute('UPDATE tasks SET body=? WHERE id=?', ('racing body', tid))
                    except sqlite3.OperationalError as exc:
                        if 'locked' not in str(exc):
                            raise
                    else:
                        raise AssertionError('writer entered claim transaction')
            except BaseException as exc:
                errors.append(exc)
            finally:
                attempted.set()
        def capture(*args, **kwargs):
            self.assertTrue(self.conn.in_transaction)
            entered.set()
            self.assertTrue(attempted.wait(5))
            return real_capture(*args, **kwargs)
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            with patch.object(admission, 'capture_execution_spec', side_effect=capture):
                claimed = kb.claim_task(self.conn, tid, expected_spec=spec)
        finally:
            entered.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNotNone(claimed)
        self.assertEqual(kb.get_task(self.conn, tid).body, 'APPROVED BODY')

    def test_postclaim_every_payload_source_remains_sealed(self):
        tid = self.create(skills=['approved'], model_override='approved-model',
                          provider_override='approved-provider', reasoning_effort='low')
        kb.add_comment(self.conn, tid, 'fixture', 'APPROVED COMMENT')
        spec = self.capture(tid, skill_prompts={'approved': 'APPROVED SKILL'})
        claimed = kb.claim_task(self.conn, tid, expected_spec=spec)
        assert claimed is not None
        original = admission.read_binding(self.conn, tid, claimed.current_run_id, claimed.claim_lock)
        assert original is not None
        # An external native writer is allowed to edit the card for a future run.
        with kbc.connect_closing(self.path) as other:
            kb.edit_task(other, tid, title='changed', body='NOT VALIDATED')
            kb.add_comment(other, tid, 'fixture', 'NOT VALIDATED COMMENT')
            kb.set_model_override(other, tid, 'changed', 'changed')
            kb.set_reasoning_effort(other, tid, 'high')
            with kb.write_txn(other):
                other.execute('UPDATE tasks SET skills=?, goal_mode=1 WHERE id=?', ('["evil"]', tid))
        self.assertEqual(admission.read_binding(self.conn, tid, claimed.current_run_id, claimed.claim_lock), original)
        admission.bind_worker(original)
        shown = admission.worker_show(self.conn, tid)
        self.assertEqual(shown['task']['body'], 'APPROVED BODY')
        self.assertIn('APPROVED COMMENT', shown['worker_context'])
        self.assertNotIn('NOT VALIDATED', shown['worker_context'])
        self.assertEqual(shown['task']['model_override'], 'approved-model')
        self.assertEqual(shown['task']['skills'], ['approved'])
        with self.assertRaisesRegex(ValueError, 'execution_task_substitution'):
            admission.worker_show(self.conn, 't_other')
        # Exercise the actual inference call site, with only the model and retry
        # runner replaced. No gateway or credential resolution is involved.
        from gateway.session_kanban import _run_task_turns, bind_worker_context
        from unittest.mock import Mock
        context = dict(execution_binding=original, task_id=tid, skills=['evil'],
            context='evil', goal_mode=False, db=str(self.path), board='default',
            run_id=claimed.current_run_id, claim_lock=claimed.claim_lock,
            workspace=str(self.root), workspaces_root=str(self.root),
            profile='fixture-worker', branch=None, tenant=None, max_runtime_seconds=None)
        frame = {'home': original['spec']['profile_id'], 'text': 'work kanban task ' + tid,
            'policy': {'source': 'kanban', 'config_json': '{}', 'kanban_json': json.dumps(context)}}
        with patch('gateway.session_kanban._watch_submitter'), patch('os.chdir'):
            bind_worker_context(frame)
        agent = Mock(session_id='fixture-only')
        agent.run_conversation.return_value = {'final_response': 'fixture'}
        with patch('agent.skill_commands.build_preloaded_skills_prompt', side_effect=AssertionError('mutable skill read')), \
             patch('agent.kanban_turn_recovery.recover_failed_kanban_turns'):
            _run_task_turns(agent, frame, [], context)
        prompt = agent.run_conversation.call_args.args[0]
        self.assertIn('APPROVED BODY', prompt)
        self.assertIn('APPROVED SKILL', prompt)
        self.assertNotIn('NOT VALIDATED', prompt)

    def test_reserved_metadata_and_completion_keep_native_lifecycle(self):
        tid = self.create()
        spec = self.capture(tid)
        claimed = kb.claim_task(self.conn, tid, expected_spec=spec)
        run_id = claimed.current_run_id
        binding = admission.read_binding(self.conn, tid, run_id, claimed.claim_lock)
        with self.assertRaisesRegex(ValueError, 'reserved_execution_metadata'):
            kb.complete_task(self.conn, tid, summary='fixture complete', expected_run_id=run_id,
                             metadata={admission.BINDING_KEY: {'spec': 'forged'}})
        self.assertEqual(kb.get_task(self.conn, tid).status, 'running')
        self.assertTrue(kb.complete_task(self.conn, tid, summary='fixture complete', expected_run_id=run_id,
                                       metadata={'worker_session_id': 'fixture-session'}))
        self.assertEqual(kb.get_task(self.conn, tid).status, 'done')
        self.assertEqual(kb.get_run(self.conn, run_id).metadata[admission.BINDING_KEY], binding)
        with self.assertRaisesRegex(ValueError, 'stale_kanban_claim'):
            admission.read_binding(self.conn, tid, run_id, claimed.claim_lock)
        with self.assertRaisesRegex(ValueError, 'reserved_execution_metadata'):
            kb.edit_task(self.conn, tid, result='changed', metadata={admission.BINDING_KEY: None})

    def test_legacy_off_control(self):
        self.set_mode('off')
        tid = self.create()
        claimed = kb.claim_task(self.conn, tid)
        self.assertIsNotNone(claimed)
        self.assertIsNone(admission.read_binding(self.conn, tid, claimed.current_run_id, claimed.claim_lock))


if __name__ == '__main__':
    unittest.main()
