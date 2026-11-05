"""Native execution admission (opt-in; no queue/schema of its own).

The trusted controller captures a spec, validates the ENTIRE returned object,
then privately hands that same object to claim_task(expected_spec=...) or
kanban_db_dispatch.dispatch_once(expected_specs={task_id: spec}). Never build the
expected value from a newly read card after validation. A digest is an equality
check, NOT approval/authentication. Protect this caller and the runtime's config,
DB and Python process with OS permissions; arbitrary SQL/code execution is outside
this boundary.

Set kanban.execution_admission: required in the SHARED kanban_home()/config.yaml
(not a task, board metadata or per-profile override). Missing = off. Required
mode denies unbound claims/dispatch and legacy gateway receipts. The controller
alone must have the supported dispatch capability; possession of a spec is not an
independent authorisation credential.

Version 1 supports native scratch and absolute dir workspaces; worktree is refused. Skill instructions
are explicit expanded bytes provided by the trusted validator (one per skill,
including sdlc-review in the review lane); no skill loader, preprocessor or file
read runs after validation. Profile configuration/system prompt/tools remain
trusted runtime authority, not card data. Attachments are locators, not sealed
file contents; callers must not treat mutable files or arbitrary tool output as
approved instructions. Auto comment steering is disabled on sealed runs.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import time

from hermes_cli.kanban_execution_limits import read_run_binding

BINDING_KEY = '_hermes_execution_binding'
MAX_SPEC_BYTES = 1024 * 1024
_LIFECYCLE = frozenset({'status', 'started_at', 'completed_at', 'claim_lock',
    'claim_expires', 'worker_pid', 'last_heartbeat_at', 'current_run_id',
    'consecutive_failures', 'last_failure_error', 'block_kind', 'block_recurrences'})
# Process-private worker copy, installed only by the adopted gateway frame.
_WORKER_BINDING = None


def canonical(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    if len(raw.encode('utf-8')) > MAX_SPEC_BYTES:
        raise ValueError('execution_spec_too_large')
    return raw


def fingerprint(spec):
    return hashlib.sha256(canonical(spec).encode('utf-8')).hexdigest()


def required(conn=None):
    """Read shared AND DB-owned policy; a different gateway home cannot weaken it.

    Native named boards inherit <root>/config.yaml. A custom DB location uses
    its parent's config.yaml in addition to kanban_home's policy. Both files
    are trusted operator configuration; either required wins.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import _main_db_file
    roots = {kb.kanban_home().resolve()}
    db = _main_db_file(conn) if conn is not None else None
    if db:
        parent = Path(db).resolve().parent
        if parent.parent.name == 'boards' and parent.parent.parent.name == 'kanban':
            roots.add(parent.parent.parent.parent)
        else:
            roots.add(parent)
    modes = [_required_at(root / 'config.yaml') for root in roots]
    return any(modes)


def _required_at(path):
    import hermes_yaml as yaml
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return False
    try:
        cfg = yaml.safe_load(raw)
        if cfg is None:
            cfg = {}
        mode = cfg.get('kanban', {}).get('execution_admission', 'off')
        if mode not in ('off', 'required'):
            raise ValueError('execution_admission_config')
        return mode == 'required'
    except (AttributeError, TypeError, yaml.YAMLError) as exc:
        raise ValueError('execution_admission_config') from exc


def require_expected(expected, conn=None):
    if expected is None and required(conn):
        raise ValueError('execution_admission_required')


def dispatch_specs(expected, conn=None):
    require_expected(expected, conn)
    if expected is None:
        return None
    detached = json.loads(canonical(expected))
    if (not isinstance(detached, dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, dict) or not v
            for k, v in detached.items())):
        raise ValueError('invalid_execution_specs')
    return detached


def _db_identity(conn):
    from hermes_cli.kanban_db_connect import _main_db_file
    path = _main_db_file(conn)
    if not path:
        raise ValueError('execution_spec_requires_file_database')
    return str(Path(path).resolve())


def capture_execution_spec(conn, task_id, *, board, skill_prompts=None, captured_at=None):
    """Return JSON data for whole-spec validation, NOT authority to execute.

    Call on a consistent read transaction for a stable preview. Claim recaptures
    inside BEGIN IMMEDIATE and compares the whole spec before any claim/run write.
    skill_prompts maps exact task skill names to already validated literal prompt
    text; extra/missing entries are rejected. Only eligible ready/todo/review cards
    can be captured. No model, shell, skill setup or filesystem mutation occurs.
    """
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if task is None or task.status not in ('todo', 'ready', 'review'):
        raise ValueError('execution_spec_task_not_eligible')
    if task.workspace_kind not in ('scratch', 'dir') or not task.assignee:
        raise ValueError('execution_spec_requires_assigned_scratch_or_dir')
    from hermes_cli.profiles import resolve_profile_env
    profile_id = str(Path(resolve_profile_env(task.assignee)).resolve())
    workspace = task.workspace_path
    if not workspace and task.workspace_kind == 'scratch':
        workspace = str(kb.workspaces_root(board=board) / task.id)
    if not workspace or not Path(workspace).is_absolute():
        raise ValueError('execution_spec_requires_absolute_workspace')
    skills = list(dict.fromkeys([*(task.skills or []), *(['sdlc-review'] if task.status == 'review' else [])]))
    prompts = {} if skill_prompts is None else skill_prompts
    if (not isinstance(prompts, dict) or set(prompts) != set(skills)
            or any(not isinstance(v, str) or not v.strip() for v in prompts.values())):
        raise ValueError('execution_spec_skill_prompts')
    stamp = int(time.time()) if captured_at is None else captured_at
    if type(stamp) is not int or stamp < 0:
        raise ValueError('execution_spec_timestamp')
    spec = {'version': 1, 'board': kb._require_slug(board), 'db': _db_identity(conn),
            'profile_id': profile_id,
            'workspace': workspace, 'captured_at': stamp, 'lane': 'review' if task.status == 'review' else 'ready',
            'task': {k: v for k, v in asdict(task).items() if k not in _LIFECYCLE},
            'skills': skills, 'skill_prompts': prompts,
            'attachments': [asdict(a) for a in kb.list_attachments(conn, task_id)],
            'context': kb.build_worker_context(conn, task_id, now=stamp,
                                               task=replace(task, status='running', workspace_path=workspace))}
    return json.loads(canonical(spec))


def check_expected(conn, task_id, expected, lane):
    """Caller holds the claim write transaction; no observer can approve here."""
    require_expected(expected, conn)
    if expected is None:
        return None
    try:
        expected = json.loads(canonical(expected))  # detach caller-owned containers
        if expected['lane'] != lane or expected['task']['id'] != task_id:
            raise ValueError('execution_spec_mismatch')
        actual = capture_execution_spec(conn, task_id, board=expected['board'],
            captured_at=expected['captured_at'], skill_prompts=expected['skill_prompts'])
        if canonical(actual) != canonical(expected):
            raise ValueError('execution_spec_mismatch')
        return actual
    except (KeyError, TypeError) as exc:
        raise ValueError('execution_spec_mismatch') from exc


def seal(conn, task_id, run_id, claim_lock, spec):
    if spec is None:
        return
    binding = {'spec': spec, 'digest': fingerprint(spec), 'task_id': task_id,
               'run_id': run_id, 'claim_lock': claim_lock}
    conn.execute('UPDATE task_runs SET metadata=? WHERE id=?',
                 (canonical({BINDING_KEY: binding}), run_id))


def read_binding(conn, task_id, run_id, claim_lock):
    """Check BOTH rows' exact live identity, then return the sealed spec, never a card."""
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    run = kb.get_run(conn, run_id)
    if (type(run_id) is not int or not claim_lock or task is None or run is None
            or task.status != 'running' or run.status != 'running'
            or task.current_run_id != run_id or task.claim_lock != claim_lock
            or run.task_id != task_id or run.claim_lock != claim_lock):
        raise ValueError('stale_kanban_claim')
    binding = read_run_binding(conn, task_id, run_id)
    if binding is None:
        require_expected(None, conn)
    return binding


def execution_task(live_task, binding):
    """Only lifecycle state is live; instruction/routing fields come from the seal."""
    if not binding:
        return live_task
    fields = dict(binding['spec']['task'], workspace_path=binding['spec']['workspace'])
    return replace(live_task, **fields)


def reject_reserved(metadata):
    if isinstance(metadata, dict) and any(str(k).startswith('_hermes_execution') for k in metadata):
        raise ValueError('reserved_execution_metadata')


def merge_run_metadata(conn, run_id, metadata):
    reject_reserved(metadata)
    row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run_id,)).fetchone()
    prior = json.loads(row[0] or '{}') if row else {}
    if BINDING_KEY not in prior:
        return metadata
    return {**(metadata or {}), BINDING_KEY: prior[BINDING_KEY]}


def verify_context(conn, context):
    binding = read_binding(conn, context['task_id'], context['run_id'], context['claim_lock'])
    if binding != context.get('execution_binding'):
        raise ValueError('invalid_execution_binding')
    return binding


def bind_worker(binding):
    global _WORKER_BINDING
    _WORKER_BINDING = json.loads(canonical(binding)) if binding else None


def worker_binding():
    return json.loads(canonical(_WORKER_BINDING)) if _WORKER_BINDING else None


def worker_show(conn, task_id):
    """Sealed workers cannot substitute another task or reread edited instructions."""
    binding = worker_binding()
    if binding is None:
        # A CLI subprocess inherits the exact run/claim pins, not the private
        # Python object. Read the immutable native seal, never the live payload.
        own_task = os.environ.get('HERMES_KANBAN_TASK')
        if not own_task:
            return None
        run = os.environ.get('HERMES_KANBAN_RUN_ID', '')
        lock = os.environ.get('HERMES_KANBAN_CLAIM_LOCK', '')
        if not run.isdecimal() or not lock:
            require_expected(None, conn)
            return None
        binding = read_run_binding(conn, own_task, int(run))
        if binding is None:
            require_expected(None, conn)
            return None
        if binding['claim_lock'] != lock:
            raise ValueError('stale_kanban_claim')
    if task_id != binding['task_id']:
        raise ValueError('execution_task_substitution')
    current = read_binding(conn, task_id, binding['run_id'], binding['claim_lock'])
    if current != binding:
        raise ValueError('invalid_execution_binding')
    spec = binding['spec']
    if not isinstance(spec.get('attachments'), list):
        raise ValueError('invalid_execution_binding')
    return {'task': spec['task'], 'worker_context': spec['context'],
            'attachments': spec['attachments'],
            'execution_digest': binding['digest'], 'sealed': True}
