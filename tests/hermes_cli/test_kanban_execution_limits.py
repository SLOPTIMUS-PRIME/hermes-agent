"""Parent-only SQLite checks for immutable per-attempt enforcement."""

import json
import os
from contextlib import closing
import time
import unittest
from unittest.mock import patch

from agent.delegation_context import is_delegated_child_process_context
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from tests.hermes_cli import test_kanban_execution_native as native_fixture


@unittest.skipIf(is_delegated_child_process_context(), 'parent-owned native fixtures')
class ExecutionLimitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.NativeAdmissionTests('test_legacy_off_control')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root
        self.conn = self.fixture.conn


    def claimed(self, *, sealed=True, runtime=60, retries=1):
        if not sealed:
            self.fixture.set_mode('off')
        tid = self.fixture.create(max_runtime_seconds=runtime, max_retries=retries)
        spec = self.fixture.capture(tid) if sealed else None
        task = kb.claim_task(self.conn, tid, expected_spec=spec)
        assert task is not None and task.current_run_id is not None
        return tid, task.current_run_id

    def enforce(self, tid, run_id, live_limit):
        with kb.write_txn(self.conn):
            self.conn.execute('UPDATE tasks SET worker_pid=?, max_runtime_seconds=? WHERE id=?',
                              (424242, live_limit, tid))
            self.conn.execute('UPDATE task_runs SET started_at=? WHERE id=?',
                              (int(time.time()) - 100, run_id))
        signals = []
        with patch.object(kb, '_pid_alive', return_value=False), \
             patch.object(dispatch, '_poll_worker_exit'), \
             patch.object(dispatch, '_worker_alive', return_value=False):
            timed_out = dispatch.enforce_max_runtime(self.conn,
                signal_fn=lambda pid, sig: signals.append((pid, sig)))
        return timed_out, signals

    def test_postclaim_runtime_increase_does_not_extend_attempt(self):
        tid, run_id = self.claimed()
        timed_out, signals = self.enforce(tid, run_id, 9999)
        self.assertEqual(timed_out, [tid])
        self.assertTrue(signals)

    def test_postclaim_runtime_removal_does_not_disable_timeout(self):
        tid, run_id = self.claimed()
        timed_out, signals = self.enforce(tid, run_id, None)
        self.assertEqual(timed_out, [tid])
        self.assertTrue(signals)

    def test_postclaim_runtime_decrease_does_not_shorten_attempt(self):
        tid, run_id = self.claimed(runtime=9999)
        timed_out, signals = self.enforce(tid, run_id, 1)
        self.assertEqual(timed_out, [])
        self.assertEqual(signals, [])

    def test_stale_budget_exhausted_finalizer_cannot_close_replacement_attempt(self):
        tid, run_a = self.claimed()
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome='timed_out', status='timed_out',
                        error='attempt A reclaimed by sweep')
            self.conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, worker_pid=NULL WHERE id=?",
                (tid,))
        spec = self.fixture.capture(tid)
        task_b = kb.claim_task(self.conn, tid, expected_spec=spec)
        assert task_b is not None and task_b.current_run_id is not None
        run_b = task_b.current_run_id
        self.assertNotEqual(run_b, run_a)
        from agent import turn_finalizer
        with patch.dict(os.environ, {'HERMES_KANBAN_TASK': tid,
                                     'HERMES_KANBAN_RUN_ID': str(run_a)}):
            turn_finalizer._record_kanban_budget_exhausted(
                tid, 60, 60, __import__('logging').getLogger('limits-fixture'))
        row = self.conn.execute(
            "SELECT status, claim_lock, current_run_id, consecutive_failures FROM tasks WHERE id=?",
            (tid,)).fetchone()
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['current_run_id'], run_b)
        self.assertIsNotNone(row['claim_lock'])
        self.assertEqual(row['consecutive_failures'], 0)
        run = kb.get_run(self.conn, run_b)
        self.assertIsNotNone(run)
        self.assertIsNone(run.ended_at)

    def test_live_budget_exhausted_finalizer_records_own_attempt(self):
        tid, run_b = self.claimed()
        assert run_b is not None
        from agent import turn_finalizer
        with patch.dict(os.environ, {'HERMES_KANBAN_TASK': tid,
                                     'HERMES_KANBAN_RUN_ID': str(run_b)}):
            turn_finalizer._record_kanban_budget_exhausted(
                tid, 60, 60, __import__('logging').getLogger('limits-fixture'))
        row = self.conn.execute(
            "SELECT status, claim_lock, current_run_id, consecutive_failures FROM tasks WHERE id=?",
            (tid,)).fetchone()
        self.assertEqual(row['status'], 'blocked')
        self.assertIsNone(row['claim_lock'])
        self.assertIsNone(row['current_run_id'])
        self.assertEqual(row['consecutive_failures'], 1)
        run = kb.get_run(self.conn, run_b)
        assert run is not None
        self.assertEqual(run.status, 'gave_up')
        self.assertIsNotNone(run.ended_at)

    def test_budget_exhausted_finalizer_skips_without_valid_run_id(self):
        tid, run_b = self.claimed()
        assert run_b is not None
        from agent import turn_finalizer
        for raw in ('', '0', '-3', 'abc', '1.5'):
            with patch.dict(os.environ, {'HERMES_KANBAN_TASK': tid,
                                         'HERMES_KANBAN_RUN_ID': raw}):
                turn_finalizer._record_kanban_budget_exhausted(
                    tid, 60, 60, __import__('logging').getLogger('limits-fixture'))
        row = self.conn.execute(
            "SELECT status, claim_lock, current_run_id, consecutive_failures FROM tasks WHERE id=?",
            (tid,)).fetchone()
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['current_run_id'], run_b)
        self.assertIsNotNone(row['claim_lock'])
        self.assertEqual(row['consecutive_failures'], 0)

    def test_failure_budget_uses_seal_after_run_has_ended(self):
        tid, run_id = self.claimed()
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome='crashed', status='crashed', error='fixture')
            self.conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, max_retries=99 WHERE id=?", (tid,))
        run = kb.get_run(self.conn, run_id)
        assert run is not None
        self.assertEqual(run.status, 'crashed')
        tripped = dispatch._record_task_failure(self.conn, tid, error='fixture', outcome='crashed',
                                               release_claim=False, end_run=False)
        self.assertTrue(tripped)
        task = kb.get_task(self.conn, tid)
        assert task is not None
        self.assertEqual(task.status, 'blocked')

    def test_protocol_budget_uses_seal_after_run_has_ended(self):
        from types import SimpleNamespace
        tid, run_id = self.claimed()
        task = kb.get_task(self.conn, tid)
        assert task is not None
        claim_lock = task.claim_lock
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome='crashed', status='crashed', error='protocol fixture')
            self.conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, max_retries=99 WHERE id=?", (tid,))
        dead = SimpleNamespace(protocol_violation=True, error_text='protocol fixture')
        with patch.object(dispatch, '_protocol_violation_streak', return_value=1):
            tripped = dispatch._account_crashes(self.conn, [(tid, 424242, claim_lock, dead, run_id)])
        self.assertEqual(tripped, [tid])

    def accounting_probe(self, original):
        """A second real SQLite writer must not see a retryable intermediate state."""
        import sqlite3
        database = self.conn.execute('PRAGMA database_list').fetchone()[2]
        def probe(*args, **kwargs):
            with closing(sqlite3.connect(database, timeout=0)) as other:
                try:
                    other.execute('BEGIN IMMEDIATE')
                except sqlite3.OperationalError as exc:
                    self.assertIn('locked', str(exc))
                else:
                    other.rollback()
                    self.fail('retry became externally claimable before failure accounting')
            return original(*args, **kwargs)
        return probe

    def test_timeout_reclaim_and_accounting_are_one_transaction(self):
        tid, run_id = self.claimed()
        with patch.object(dispatch, '_record_task_failure',
                          side_effect=self.accounting_probe(dispatch._record_task_failure)):
            self.assertEqual(self.enforce(tid, run_id, 60)[0], [tid])
        task = kb.get_task(self.conn, tid)
        assert task is not None
        self.assertEqual(task.status, 'blocked')

    def test_crash_reclaim_and_accounting_are_one_transaction(self):
        tid, run_id = self.claimed()
        with kb.write_txn(self.conn):
            self.conn.execute('UPDATE tasks SET worker_pid=?, started_at=? WHERE id=?',
                              (424242, int(time.time()) - 600, tid))
        with patch.object(dispatch, '_worker_alive', return_value=False), \
             patch.object(dispatch, '_classify_worker_exit', return_value=('nonzero_exit', 1)), \
             patch.object(dispatch, '_record_task_failure',
                          side_effect=self.accounting_probe(dispatch._record_task_failure)):
            self.assertEqual(dispatch.detect_crashed_workers(self.conn), [tid])
        task = kb.get_task(self.conn, tid)
        assert task is not None
        self.assertEqual(task.status, 'blocked')

    def test_ttl_reclaim_and_accounting_are_one_transaction(self):
        tid, run_id = self.claimed()
        with kb.write_txn(self.conn):
            self.conn.execute('UPDATE tasks SET claim_expires=? WHERE id=?',
                              (int(time.time()) - 600, tid))
        with patch.object(kb, '_record_task_failure',
                          side_effect=self.accounting_probe(dispatch._record_task_failure)):
            self.assertEqual(kb.release_stale_claims(self.conn), 1)
        task = kb.get_task(self.conn, tid)
        assert task is not None
        self.assertEqual(task.status, 'blocked')

    def test_delayed_crash_uses_exact_run_not_reused_claimer(self):
        from types import SimpleNamespace
        tid, old_run = self.claimed()
        old = kb.get_run(self.conn, old_run)
        assert old is not None
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome='crashed', status='crashed', error='old')
            self.conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        replacement = kb.claim_task(self.conn, tid, expected_spec=self.fixture.capture(tid))
        assert replacement is not None
        self.assertEqual(replacement.claim_lock, old.claim_lock)
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome='crashed', status='crashed', error='new')
            self.conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        before = dict(self.conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        dead = SimpleNamespace(protocol_violation=True, error_text='old protocol failure')
        with patch.object(dispatch, '_protocol_violation_streak', return_value=1):
            self.assertEqual(dispatch._account_crashes(self.conn,
                [(tid, 424242, old.claim_lock, dead, old_run)]), [])
        self.assertEqual(dict(self.conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()), before)

    def test_old_failure_cannot_mutate_replacement_run(self):
        tid, old_run = self.claimed()
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome='crashed', status='crashed', error='old')
            self.conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        replacement = kb.claim_task(self.conn, tid, expected_spec=self.fixture.capture(tid))
        assert replacement is not None and replacement.current_run_id is not None
        before = dict(self.conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        self.assertFalse(dispatch._record_task_failure(self.conn, tid, 'late old failure',
            outcome='spawn_failed', release_claim=True, end_run=True, expected_run_id=old_run))
        self.assertEqual(dict(self.conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()), before)
        run = kb.get_run(self.conn, replacement.current_run_id)
        assert run is not None
        self.assertEqual(run.status, 'running')

    def test_default_off_preserves_mutable_runtime_policy(self):
        tid, run_id = self.claimed(sealed=False)
        self.assertEqual(self.enforce(tid, run_id, 9999), ([], []))

    def test_historical_seal_reader_checks_identity_and_integrity(self):
        from hermes_cli.kanban_execution_limits import read_run_binding
        tid, run_id = self.claimed()
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome='crashed', status='crashed', error='fixture')
        self.assertEqual(read_run_binding(self.conn, tid, run_id)['run_id'], run_id)
        with self.assertRaises(ValueError):
            read_run_binding(self.conn, 'foreign-task', run_id)
        run = kb.get_run(self.conn, run_id)
        assert run is not None and run.metadata is not None
        metadata = run.metadata
        metadata['_hermes_execution_binding']['spec']['task']['max_retries'] = 99
        with kb.write_txn(self.conn):
            self.conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(metadata), run_id))
        with self.assertRaisesRegex(ValueError, 'invalid_execution_binding'):
            read_run_binding(self.conn, tid, run_id)


if __name__ == '__main__':
    unittest.main()
