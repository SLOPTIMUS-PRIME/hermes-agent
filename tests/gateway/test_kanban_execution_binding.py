"""Offline gateway/worker admission tests: no owner, model or native board."""
import contextlib
import copy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_execution as admission
from gateway import session_kanban
from gateway.session_policy import LocalSessionPolicy
from tests.hermes_cli.test_kanban_execution_admission import task


def sealed_fixture():
    original = task(skills=['approved'], model_override='approved-model', provider_override='approved-provider',
                    reasoning_effort='low', body='APPROVED BODY')
    conn = Mock()
    with patch.object(kb, 'get_task', return_value=original), \
         patch.object(kb, 'list_attachments', return_value=[]), \
         patch.object(kb, 'build_worker_context', return_value='APPROVED CONTEXT'), \
         patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'), \
         patch('hermes_cli.profiles.resolve_profile_env', return_value='/fixture/profile'):
        spec = admission.capture_execution_spec(conn, original.id, board='test', captured_at=100,
                    skill_prompts={'approved': 'APPROVED SKILL BYTES'})
    binding = {'spec': spec, 'digest': admission.fingerprint(spec), 'task_id': original.id,
               'run_id': 7, 'claim_lock': 'claim-7'}
    live = task(status='running', current_run_id=7, claim_lock='claim-7',
                body='MUTATED BODY', skills=['evil'], model_override='evil',
                provider_override='evil', reasoning_effort='high')
    run = SimpleNamespace(id=7, task_id=live.id, status='running', claim_lock='claim-7',
                          metadata={admission.BINDING_KEY: binding})
    return conn, live, run, binding


class GatewayBindingTests(unittest.TestCase):
    def tearDown(self):
        admission.bind_worker(None)

    def test_gateway_uses_sealed_body_context_skills_and_overrides(self):
        conn, live, run, binding = sealed_fixture()
        conn.execute.return_value.fetchone.side_effect = [
            dict(status='running', task_id=live.id, claim_lock='claim-7'), None]
        connection = SimpleNamespace(native_owner=True,
            authority=SimpleNamespace(profile_id='/fixture/profile'),
            actor=SimpleNamespace(profile_id='/fixture/profile', capabilities={'session:create'}))
        policy = LocalSessionPolicy('cli', 'cli', '/fixture', 'approved-model', (), '{}', '{}', '{}')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(session_kanban.sqlite3, 'connect', return_value=conn))
            stack.enter_context(patch.object(kb, 'kanban_db_path', return_value=Path('/fixture/kanban.db')))
            stack.enter_context(patch.object(kb, 'get_task', return_value=live))
            stack.enter_context(patch.object(kb, 'get_run', return_value=run))
            stack.enter_context(patch.object(kb, 'build_worker_context', return_value='MUTATED CONTEXT'))
            stack.enter_context(patch.object(kb, '_retry_status_for_run', return_value='ready'))
            stack.enter_context(patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'))
            stack.enter_context(patch('hermes_cli.profiles.resolve_profile_env', return_value='/fixture/profile'))
            build = stack.enter_context(patch('gateway.session_policy.build_policy', return_value=policy))
            result, _ = session_kanban.build_kanban_policy(connection,
                dict(board='test', task_id=live.id, run_id=7, claim_lock='claim-7'), {})
        context = json.loads(result.kanban_json)
        self.assertEqual(context['context'], 'APPROVED CONTEXT')
        self.assertEqual(context['skills'], ['approved'])
        self.assertIn('APPROVED BODY', context['goal_text'])
        self.assertEqual(build.call_args.args[0]['model'], 'approved-model')
        self.assertEqual(build.call_args.args[0]['provider'], 'approved-provider')
        self.assertEqual(build.call_args.args[0]['reasoning'], 'low')
        self.assertEqual(context['execution_binding'], binding)

    def test_inference_never_reloads_mutable_skill_files(self):
        conn, live, run, binding = sealed_fixture()
        admission.bind_worker(binding)
        agent = Mock(session_id='fake-only')
        agent.run_conversation.return_value = {'final_response': 'fixture', 'completed': True}
        context = dict(task_id=live.id, run_id=7, claim_lock='claim-7', db='/fixture/kanban.db',
                       skills=['approved'], context='APPROVED CONTEXT',
                       goal_mode=False, execution_binding=binding)
        with patch('agent.skill_commands.build_preloaded_skills_prompt',
                   return_value=('MUTATED SKILL FILE', ['evil'], [])) as loader, \
             patch('agent.kanban_turn_recovery.recover_failed_kanban_turns'), \
             patch.object(session_kanban.sqlite3, 'connect', return_value=conn), \
             patch.object(kb, 'get_task', return_value=live), \
             patch.object(kb, 'get_run', return_value=run), \
             patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'), \
             patch.object(session_kanban, '_bound_claim_is_live', return_value=True):
            session_kanban._run_task_turns(agent, {'text': 'work kanban task t_sealed'}, [], context)
        loader.assert_not_called()
        prompt = agent.run_conversation.call_args.args[0]
        self.assertIn('APPROVED SKILL BYTES', prompt)
        self.assertNotIn('MUTATED', prompt)

    def test_later_show_cannot_substitute_live_task_or_context(self):
        conn, live, run, binding = sealed_fixture()
        admission.bind_worker(binding)
        from tools import kanban_tools
        with patch.object(kanban_tools, '_board', return_value=contextlib.nullcontext((kb, conn))), \
             patch.object(kb, 'get_task', return_value=live), \
             patch.object(kb, 'get_run', return_value=run), \
             patch.object(admission, '_db_identity', return_value='/fixture/kanban.db'), \
             patch.object(kb, 'build_worker_context', return_value='MUTATED CONTEXT'), \
             patch.object(kb, 'parent_ids', return_value=[]), \
             patch.object(kb, 'child_ids', return_value=[]), \
             patch.object(kb, 'unsatisfied_parents', return_value=[]), \
             patch.object(kb, 'list_comments', return_value=[]), \
             patch.object(kb, 'list_events', return_value=[]), \
             patch.object(kb, 'list_runs', return_value=[]):
            result = json.loads(kanban_tools._handle_show({'task_id': live.id}))
            substituted = json.loads(kanban_tools._handle_show({'task_id': 't_foreign'}))
        self.assertEqual(result['task']['body'], 'APPROVED BODY')
        self.assertEqual(result['worker_context'], 'APPROVED CONTEXT')
        self.assertIn('execution_task_substitution', json.dumps(substituted))


if __name__ == '__main__':
    unittest.main()
