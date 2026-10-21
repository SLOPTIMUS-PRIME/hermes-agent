"""A user prompt queued after Stop restores goal and completion admission.

The queued path must clear the old session cancellation latch, just as an
ordinary idle prompt does, without clearing a newer Stop racing the drain.
"""
import queue
import types

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_queued_prompt_persistence import _busy, _desktop_session, _run_turn
from tools.process_registry import process_registry
from tui_gateway import server


@pytest.fixture()
def stopped_session(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    session["agent"] = types.SimpleNamespace(interrupt=lambda: None)
    session["_run_thread"] = types.SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    _busy(session)
    server._interrupt_session_turn(sid, session)
    assert session["running"] and session["_turn_cancel_requested"]
    try:
        yield sid, key, session, db
    finally:
        server._sessions.pop(sid, None)
        db.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_queued_user_after_stop_restores_automatic_turns(monkeypatch, stopped_session, isolated):
    sid, key, session, db = stopped_session
    submitted = []
    response = server._handle_busy_submit("user", sid, session, "inspect attachment", None, queued=True)
    assert response["result"]["status"] == "queued"
    assert session["_turn_cancel_requested"]  # Stop still belongs to the winding-down turn.
    session["running"] = False
    server._clear_inflight_turn(session)

    def run(_rid, _sid, current, text, **_kwargs):
        submitted.append(text)
        _run_turn(current, db, key, text, "work remains")
        current["running"] = False
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", run)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: isolated)
    monkeypatch.setattr(server, "_submit_prompt_to_compute_host",
                        lambda *args, **kwargs: run(*args, **kwargs) and {"result": {"status": "started"}})
    assert server._drain_queued_prompt("drain", sid, session)
    assert submitted == ["inspect attachment"]
    server._run_post_turn_followups("goal", sid, session, {}, "continue toward goal")
    assert submitted == ["inspect attachment", "continue toward goal"]
    assert server._notif_claim_turn(session) is True


@pytest.mark.parametrize("stop_at", ["after_claim", "before_dispatch"])
def test_new_stop_during_queue_drain_still_blocks_automatic_work(monkeypatch, stopped_session, stop_at):
    sid, _key, session, _db = stopped_session
    server._enqueue_prompt(session, "follow-up", None)
    session["running"] = False
    stopped_again = False
    original_submit = server._run_prompt_submit

    def stop_once():
        nonlocal stopped_again
        if not stopped_again:
            stopped_again = True
            server._interrupt_session_turn(sid, session)

    if stop_at == "after_claim":
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: stop_once() or False)
    else:
        def stop_before_dispatch(*args, **kwargs):
            stop_once()
            return original_submit(*args, **kwargs)
        monkeypatch.setattr(server, "_run_prompt_submit", stop_before_dispatch)

    assert server._drain_queued_prompt("drain", sid, session)
    assert stopped_again and session["_turn_cancel_requested"]
    assert not session.get("queued_prompt")  # Never resurrect the prompt the new Stop cancelled.
    assert not session.get("queued_prompts")
    assert session["running"] is False
    server._run_post_turn_followups("goal", sid, session, {}, "must stay stopped")
    assert server._notif_claim_turn(session) is False


def test_stop_without_a_new_user_prompt_keeps_automatic_work_blocked(stopped_session):
    sid, _key, session, _db = stopped_session
    session["running"] = False
    assert server._drain_queued_prompt("drain", sid, session) is False
    server._run_post_turn_followups("goal", sid, session, {}, "must stay stopped")
    assert session["_turn_cancel_requested"]
    assert server._notif_claim_turn(session) is False
