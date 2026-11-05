"""Sealed handoff/read surfaces against deterministic in-memory seams only.

Run with unittest under delegation: pytest's global fixtures scrub the inherited
fence. No board, model, config, credential or network is used here. The permitted
worker guard is a fake *only* alongside a fake board; separate tests exercise the
real delegated-entrypoint rejection. The process fence is never cleared/patched.
"""
import argparse
import contextlib
import copy
import io
import json
import os
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_execution as execution
from tools import kanban_tools as tools


class SealedSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.fence = os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')
        self.addCleanup(lambda: self.assertEqual(
            self.fence, os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')))
        self.conn = object()
        self.events = []
        self.live = SimpleNamespace(id='t_sealed', title='Approved title',
                                    body='Approved body', goal_mode=True,
                                    status='running', last_failure_error=None)
        self.sealed: Any = {'task': copy.deepcopy(vars(self.live)),
                       'worker_context': 'Approved context', 'attachments': [],
                       'execution_digest': 'fixture-digest', 'sealed': True}
        self.db = SimpleNamespace(
            get_task=Mock(side_effect=lambda *_: self.live),
            complete_task=Mock(side_effect=self.complete),
            request_review=Mock(side_effect=self.review),
            latest_run=Mock(return_value=SimpleNamespace(id=7, summary='evidence')),
            list_attachments=Mock(return_value=[]),
            unsatisfied_parents=Mock(return_value=[]),
            ArtifactPreservationError=kb.ArtifactPreservationError,
            LiveClaimError=kb.LiveClaimError,
            HallucinatedCardsError=kb.HallucinatedCardsError,
            EmptyCompletionError=kb.EmptyCompletionError)
        # Hard stop if a missed seam tries to open any native connection.
        self.stack.enter_context(patch.object(kbc, 'connect',
            side_effect=AssertionError('native connection forbidden')))
        self.stack.enter_context(patch.object(kbc, 'connect_closing',
            return_value=contextlib.nullcontext(self.conn)))
        self.stack.enter_context(patch.object(tools, '_board',
            return_value=contextlib.nullcontext((self.db, self.conn))))
        self.stack.enter_context(patch.object(cli, 'kb', self.db))
        self.stack.enter_context(patch.dict(os.environ, {
            'HERMES_KANBAN_TASK': self.live.id, 'HERMES_KANBAN_RUN_ID': '7',
            'HERMES_SESSION_ID': ''}))
        self.show = self.stack.enter_context(patch.object(execution, 'worker_show',
            side_effect=lambda *_: copy.deepcopy(self.sealed)))
        self.judge = Mock(side_effect=self.judgment)
        self.stack.enter_context(patch.object(tools, 'judge_goal', self.judge))
        self.stack.enter_context(patch('hermes_cli.goals.judge_goal', self.judge))
        self.stack.enter_context(patch.object(tools, '_goal_judge_available', return_value=True))
        self.stack.enter_context(patch('agent.auxiliary_client.get_text_auxiliary_client',
            return_value=(object(), 'fixture-judge')))

    def complete(self, *args, **kwargs):
        self.events.append('mutation')
        self.live.status = 'done'
        return True

    def review(self, *args, **kwargs):
        self.events.append('mutation')
        self.live.status = 'review'
        return True, None

    def judgment(self, **kwargs):
        self.events.append('judge')
        return 'done', 'fixture accepted', False, None, False

    def fake_worker_guard(self, tool_name, args):
        # Simulate an already-authorized worker over fake storage, while keeping
        # actual task scoping and run pins. This grants no real board authority.
        self.events.append('scope')
        tid = tools._require_task_id(args)
        tools._enforce_worker_task_ownership(tid)
        if tools._worker_run_id(tid) is None:
            raise tools._Reject('unbound fixture worker')
        return tid

    def invoke(self, surface, handoff, tid='t_sealed', summary='evidence') -> Any:
        if surface == 'tool':
            handler = {'complete': tools._handle_complete,
                       'request-review': tools._handle_request_review}[handoff]
            with patch.object(tools, '_worker_guard', side_effect=self.fake_worker_guard):
                return json.loads(handler({'task_id': tid, 'summary': summary}))
        args = argparse.Namespace(task_ids=[tid], task_id=tid, summary=summary,
                                  result=None, metadata=None, force=False, reviewer=None)
        handler = {'complete': cli._cmd_complete,
                   'request-review': cli._cmd_request_review}[handoff]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return handler(args)

    def assert_success(self, surface, result):
        if surface == 'tool':
            self.assertTrue(result.get('ok'), result)
        else:
            self.assertEqual(result, 0)

    def check_goal_source(self, surface):
        for handoff in ('complete', 'request-review'):
            for sealed_mode, live_mode in ((True, True), (True, False), (False, True), (False, False)):
                with self.subTest(handoff=handoff, sealed_mode=sealed_mode, live_mode=live_mode):
                    self.sealed['task']['goal_mode'] = sealed_mode
                    self.live.goal_mode = live_mode
                    self.live.title, self.live.body = 'POST-CLAIM TITLE', 'POST-CLAIM BODY'
                    self.live.status = 'running'
                    self.judge.reset_mock()
                    self.events.clear()
                    result = self.invoke(surface, handoff)
                    self.assert_success(surface, result)
                    if sealed_mode:
                        self.judge.assert_called_once_with(
                            goal='Approved title\n\nApproved body', last_response='evidence')
                        self.assertLess(self.events.index('judge'), self.events.index('mutation'))
                    else:
                        self.judge.assert_not_called()

    def test_tool_handoffs_use_sealed_title_body_and_goal_policy(self):
        self.check_goal_source('tool')

    def test_cli_handoffs_use_sealed_title_body_and_goal_policy(self):
        self.check_goal_source('cli')

    def attachments(self, surface, want_json=True) -> Any:
        if surface == 'tool':
            return json.loads(tools._handle_attachments({'task_id': self.live.id}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli._cmd_attachments(argparse.Namespace(task_id=self.live.id, json=want_json))
        self.assertEqual(rc, 0)
        return json.loads(output.getvalue()) if want_json else output.getvalue()

    def check_attachments(self, surface):
        approved = dict(id=1, task_id='t_sealed', filename='Approved résumé.txt',
                        stored_path='/fixture/approved.txt', content_type='text/plain',
                        size=10, uploaded_by='owner', created_at=100)
        changed = dict(approved, filename='POST-CLAIM NAME', stored_path='/fixture/changed')
        added = dict(approved, id=2, filename='NEW INSTRUCTIONS', stored_path='/fixture/new')
        self.sealed['attachments'] = [approved]
        self.db.list_attachments.return_value = [SimpleNamespace(**changed), SimpleNamespace(**added)]
        result = self.attachments(surface)
        expected = [{key: approved[key] for key in tools._ATTACHMENT_FIELDS}]
        self.assertEqual(result, {'ok': True, 'task_id': self.live.id, 'attachments': expected}
                         if surface == 'tool' else expected)
        self.db.list_attachments.assert_not_called()
        if surface == 'cli':
            text = self.attachments(surface, want_json=False)
            self.assertIn('Approved résumé.txt', text)
            self.assertIn('/fixture/approved.txt', text)
            self.assertNotIn('POST-CLAIM', text)
            self.assertNotIn('/fixture/new', text)
        self.sealed['attachments'] = []
        result = self.attachments(surface)
        self.assertEqual(result['attachments'] if surface == 'tool' else result, [])
        self.db.list_attachments.assert_not_called()

    def test_tool_attachments_use_only_sealed_selection(self):
        self.check_attachments('tool')

    def test_cli_attachments_use_only_sealed_selection(self):
        self.check_attachments('cli')

    def test_cross_task_handoffs_reject_before_projection_or_judge(self):
        for surface in ('tool', 'cli'):
            for handoff in ('complete', 'request-review'):
                with self.subTest(surface=surface, handoff=handoff):
                    self.show.reset_mock()
                    self.judge.reset_mock()
                    if surface == 'cli':
                        with self.assertRaisesRegex(ValueError, 'scoped to task'):
                            self.invoke(surface, handoff, tid='t_foreign')
                    else:
                        result = self.invoke(surface, handoff, tid='t_foreign')
                        self.assertIn('scoped to task', result['error'])
                    self.show.assert_not_called()
                    self.judge.assert_not_called()
                    self.db.complete_task.assert_not_called()
                    self.db.request_review.assert_not_called()

    def test_binding_errors_fail_closed_before_judge_or_mutation(self):
        for error in ('stale_kanban_claim', 'invalid_execution_binding',
                      'execution_admission_required', 'execution_task_substitution'):
            self.show.side_effect = ValueError(error)
            for goal_mode in (True, False):
                self.live.goal_mode = goal_mode
                for surface in ('tool', 'cli'):
                    for handoff in ('complete', 'request-review'):
                        with self.subTest(error=error, goal_mode=goal_mode,
                                          surface=surface, handoff=handoff):
                            if surface == 'cli':
                                with self.assertRaisesRegex(ValueError, error):
                                    self.invoke(surface, handoff)
                            else:
                                result = self.invoke(surface, handoff)
                                self.assertIn(error, result['error'])
                            self.judge.assert_not_called()
                            self.db.complete_task.assert_not_called()
                            self.db.request_review.assert_not_called()
                            self.db.get_task.assert_not_called()

    def test_legacy_off_handoffs_keep_live_goal_policy(self):
        self.sealed = None
        for surface in ('tool', 'cli'):
            for handoff in ('complete', 'request-review'):
                for goal_mode in (True, False):
                    with self.subTest(surface=surface, handoff=handoff, goal_mode=goal_mode):
                        self.live.goal_mode = goal_mode
                        self.live.title, self.live.body = 'Live title', 'Live body'
                        self.live.status = 'running'
                        self.judge.reset_mock()
                        self.assert_success(surface, self.invoke(surface, handoff))
                        if goal_mode:
                            self.judge.assert_called_once_with(
                                goal='Live title\n\nLive body', last_response='evidence')
                        else:
                            self.judge.assert_not_called()
                        mutator = self.db.complete_task if handoff == 'complete' else self.db.request_review
                        self.assertEqual(mutator.call_args.kwargs['expected_run_id'], 7)

    def test_missing_sealed_attachments_never_falls_back_to_live(self):
        self.sealed.pop('attachments')
        result = self.attachments('tool')
        self.assertIn('invalid_execution_attachments', result['error'])
        with self.assertRaisesRegex(ValueError, 'invalid_execution_attachments'):
            self.attachments('cli')
        self.db.list_attachments.assert_not_called()
        self.db.get_task.assert_not_called()

    def test_attachment_binding_errors_never_fall_back_to_live(self):
        self.show.side_effect = ValueError('execution_task_substitution')
        result = self.attachments('tool')
        self.assertIn('execution_task_substitution', result['error'])
        with self.assertRaisesRegex(ValueError, 'execution_task_substitution'):
            self.attachments('cli')
        self.db.list_attachments.assert_not_called()
        self.db.get_task.assert_not_called()

    def test_legacy_off_attachments_keep_live_selection(self):
        self.sealed = None
        attachment = dict(id=5, filename='live.txt', content_type=None, size=4,
                          uploaded_by=None, stored_path='/fixture/live.txt', created_at=100)
        self.db.list_attachments.return_value = [SimpleNamespace(**attachment)]
        self.assertEqual(self.attachments('tool'), {
            'ok': True, 'task_id': self.live.id, 'attachments': [attachment]})
        self.assertEqual(self.attachments('cli'), [attachment])
        text = self.attachments('cli', want_json=False)
        self.assertIn('live.txt', text)
        self.assertIn('(4 bytes, -, by -)', text)

    def test_real_delegation_fences_deny_before_storage_and_judge(self):
        from agent.delegation_context import delegated_child_context
        self.db.kanban_home = Mock(return_value='/fixture/board')
        self.db.kanban_db_path = Mock(return_value='/fixture/board/kanban.db')
        with delegated_child_context():
            for handoff, handler in (('complete', tools._handle_complete),
                                     ('request-review', tools._handle_request_review)):
                with self.subTest(handoff=handoff):
                    # No fake worker guard here: the real predicate must reject.
                    result = json.loads(handler({'task_id': self.live.id, 'summary': 'evidence'}))
                    self.assertIn('delegate_task child', result['error'])
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.kanban_command(argparse.Namespace(
                            kanban_action=handoff, board=None)), 1)
        self.show.assert_not_called()
        self.judge.assert_not_called()
        self.db.get_task.assert_not_called()
        self.db.complete_task.assert_not_called()
        self.db.request_review.assert_not_called()

    def test_genuine_judge_rejection_keeps_native_handoff_unmodified(self):
        for surface in ('tool', 'cli'):
            for handoff in ('complete', 'request-review'):
                for verdict in ('blocked', 'continue'):
                    with self.subTest(surface=surface, handoff=handoff, verdict=verdict):
                        self.judge.side_effect = None
                        self.judge.return_value = verdict, 'criteria not met', False, None, False
                        result = self.invoke(surface, handoff)
                        if surface == 'tool':
                            self.assertIn('criteria not met', result['error'])
                        else:
                            self.assertEqual(result, 1)
                        self.assertEqual(self.live.status, 'running')
                        self.db.complete_task.assert_not_called()
                        self.db.request_review.assert_not_called()

    def test_tool_evidence_redaction_and_native_response_shapes_preserved(self):
        secret = 'ghp_' + 'A' * 40  # Synthetic recognisable secret pattern only.
        for handoff in ('complete', 'request-review'):
            with self.subTest(handoff=handoff):
                result = self.invoke('tool', handoff, summary='Evidence ' + secret)
                self.assert_success('tool', result)
                self.assertNotIn(secret, self.judge.call_args.kwargs['last_response'])
                mutator = self.db.complete_task if handoff == 'complete' else self.db.request_review
                self.assertNotIn(secret, mutator.call_args.kwargs['summary'])
                self.assertEqual(mutator.call_args.kwargs['expected_run_id'], 7)
                expected = {'ok': True, 'task_id': self.live.id, 'run_id': 7}
                expected.update({'attachments': []} if handoff == 'complete' else {'status': 'review'})
                self.assertEqual(result, expected)

    def test_unavailable_judge_does_not_bypass_binding_validation(self):
        self.show.side_effect = ValueError('invalid_execution_binding')
        with patch.object(tools, '_goal_judge_available', return_value=False), patch(
                'agent.auxiliary_client.get_text_auxiliary_client', return_value=(None, None)):
            for surface in ('tool', 'cli'):
                for handoff in ('complete', 'request-review'):
                    with self.subTest(surface=surface, handoff=handoff):
                        if surface == 'tool':
                            self.assertIn('invalid_execution_binding', self.invoke(surface, handoff)['error'])
                        else:
                            with self.assertRaisesRegex(ValueError, 'invalid_execution_binding'):
                                self.invoke(surface, handoff)
        self.judge.assert_not_called()
        self.db.complete_task.assert_not_called()
        self.db.request_review.assert_not_called()


if __name__ == '__main__':
    unittest.main()
