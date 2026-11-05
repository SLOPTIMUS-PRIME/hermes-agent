"""Read native run seals for lifecycle enforcement, including ended attempts."""
from __future__ import annotations

import json


def read_run_binding(conn, task_id, run_id):
    """Verify a stored attempt, without claiming it is still executable.

    Terminal receipt recovery and failure accounting need historical identity.
    Callers that may execute MUST separately enforce the current running claim.
    A genuinely unsealed legacy run returns None; malformed sealed data fails.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_execution import BINDING_KEY, canonical, fingerprint, _db_identity

    if type(run_id) is not int or run_id <= 0:
        raise ValueError('stale_kanban_claim')
    run = kb.get_run(conn, run_id)
    if run is None or run.id != run_id or run.task_id != task_id:
        raise ValueError('stale_kanban_claim')
    metadata = run.metadata or {}
    if not isinstance(metadata, dict):
        raise ValueError('invalid_execution_binding')
    if BINDING_KEY not in metadata:
        return None
    binding = metadata[BINDING_KEY]
    try:
        if (not run.claim_lock or binding['task_id'] != task_id
                or type(binding['run_id']) is not int or binding['run_id'] != run_id
                or binding['claim_lock'] != run.claim_lock
                or binding['spec']['task']['id'] != task_id
                or binding['spec']['db'] != _db_identity(conn)
                or binding['digest'] != fingerprint(binding['spec'])):
            raise ValueError('invalid_execution_binding')
        return json.loads(canonical(binding))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('invalid_execution_binding') from exc


def attempt_limit(conn, task_id, key, live_value, *, run_id=None):
    """Use sealed attempt limits; preserve live-policy semantics for legacy runs."""
    if key not in ('max_runtime_seconds', 'max_retries'):
        raise ValueError('invalid_execution_limit')
    if run_id is None:
        row = conn.execute('SELECT current_run_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        run_id = row[0] if row else None
    if run_id is None:
        # Native _end_run clears tasks.current_run_id before the supervisor
        # accounts the failure. The last ended attempt still owns that budget.
        from hermes_cli import kanban_db as kb
        latest = kb.latest_run(conn, task_id)
        run_id = latest.id if latest is not None else None
    if run_id is None:
        return live_value
    binding = read_run_binding(conn, task_id, run_id)
    if binding is None:
        return live_value
    try:
        value = binding['spec']['task'][key]
    except (KeyError, TypeError) as exc:
        raise ValueError('invalid_execution_binding') from exc
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError('invalid_execution_limit')
    return value
