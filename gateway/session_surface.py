"""Per-turn client surface (floating HUD, live voice) carried through canonical admission.

A surface is a fact about ONE turn, never about the session: the same session alternates
between the app window, the HUD and a spoken delegation. It is committed with the admission
row and reaches the model as a note on the user message (api_content sidecar), so the
persisted user row stays the words the user typed and the frozen system prefix is untouched.

``client`` is the label the submitting connection announced (``client.capabilities``), stamped by
the owner, never sent on ``prompt.submit``. It is attribution only: no note, route or permission
reads it, so a client-only commit reaches the model exactly as no surface does.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from gateway.session_client import client_problem, normalized_client
from persistence.core.errors import AuthorityRefusal

SURFACES = frozenset({'hud', 'voice-live', 'desktop'})
# Session surfaces whose frozen prompt says "plain terminal, MEDIA: tags are not intercepted".
_TERMINAL_PLATFORMS = frozenset({'cli', 'tui'})
DESKTOP_VIEWER_NOTE = (
    "[System: The user is reading and answering this conversation in the Hermes desktop app, not a "
    "terminal, so the terminal guidance in the system prompt does not apply to this reply. Markdown "
    "renders. To give the user a file, write MEDIA:/absolute/path/to/file on its own line: it shows as "
    "a card with Download and preview buttons. Do not give a bare path or say files cannot be attached.]"
)
_VOICE_CONTEXT_LIMIT = 6000
_SUBMIT_FIELDS = ('surface', 'voice_context', 'interrupted', 'voice_turn')
_surface_turn = ContextVar('surface_turn', default=None)


def submit_surface_fields(params):
    """The raw per-turn surface fields of a ``prompt.submit`` request, forwarded to admission."""
    return {key: params[key] for key in _SUBMIT_FIELDS if key in params}


def mark_desktop_viewer(authority, actor, source):
    """Remember that *actor*'s connection is the Desktop app: it names itself when it opens a chat
    (``session.resume source``). Read by ``turn_surface``; forgotten when the connection closes."""
    if source in ('desktop', 'gui'):
        authority.__dict__.setdefault('desktop_viewers', set()).add(actor.transport_id)


def forget_desktop_viewer(authority, actor):
    getattr(authority, 'desktop_viewers', set()).discard(actor.transport_id)


def _session_platform(authority, live):
    """The frozen creation platform of a local session (``cli``, ``desktop``, ...), or None."""
    from gateway.session_policy import policy_for_source
    try:
        policy = policy_for_source(authority.runner, getattr(live, 'source', None))
    except AuthorityRefusal:  # a route whose frozen policy is not restored: the turn refuses it
        return None
    return getattr(policy, 'platform', None)


def turn_surface(authority, ref, payload):
    """The surface facts an admitted turn runs with: its committed ``surface_v1``, plus ``desktop``
    when it names no surface and the Desktop app has this terminal-created chat open (a one-shot or
    CLI session continued in the app). Such a chat keeps its creation platform, so its prompt tells
    the model MEDIA: tags print as literal text, while the app renders them as file cards. Decided
    when the turn runs and never committed: the admission row stays what the client sent."""
    committed = payload.get('surface_v1')
    if committed and committed.get('surface'):
        return committed
    live = authority.sessions.get(ref.session_id)
    viewers = getattr(authority, 'desktop_viewers', ())
    if live is None or not any(getattr(actor, 'transport_id', None) in viewers
                               for actor in live.subscribers.values()):
        return committed
    if _session_platform(authority, live) not in _TERMINAL_PLATFORMS:
        return committed
    return {**(committed or {}), 'surface': 'desktop'}


def admit_surface(params):
    """Wire ``surface`` / ``voice_context`` / ``interrupted`` / ``voice_turn`` and the owner-stamped
    ``client`` -> committed ``surface_v1`` (``{}`` when absent). ``voice_turn`` marks a spoken voice-mode
    turn: it runs on ``auxiliary.voice_chat``."""
    surface, context, interrupted, voice_turn = (params.get(key) for key in _SUBMIT_FIELDS)
    client = params.get('client')
    if surface is not None and surface not in SURFACES:
        raise AuthorityRefusal('invalid_params')
    # The spoken transcript only makes sense for a live-voice delegation; anywhere else it is
    # a client smuggling model input past the persisted row.
    if context is not None and (surface != 'voice-live' or not isinstance(context, str)):
        raise AuthorityRefusal('invalid_params')
    if any(flag is not None and type(flag) is not bool for flag in (interrupted, voice_turn)):
        raise AuthorityRefusal('invalid_params')
    if client is not None and client_problem(client) is not None:
        raise AuthorityRefusal('invalid_params')
    committed = {}
    if surface:
        committed['surface'] = surface
    if context:
        committed['voice_context'] = context[:_VOICE_CONTEXT_LIMIT]
    if interrupted:
        committed['interrupted'] = True
    if voice_turn:
        committed['voice_turn'] = True
    if client is not None:
        committed['client'] = normalized_client(client)
    return {'surface_v1': committed} if committed else {}


def restore_surface(committed):
    """A committed ``surface_v1`` arriving across a process boundary (managed worker), re-checked by
    the admission validator: only an object admission itself could have committed is accepted."""
    if (not isinstance(committed, dict) or not committed or set(committed) - {*_SUBMIT_FIELDS, 'client'}
            or admit_surface(committed).get('surface_v1', {}) != committed):
        raise AuthorityRefusal('invalid_params')
    return committed


def _hud_note(committed, valid_tool_names):
    from agent.prompt_builder import hud_surface_note
    return hud_surface_note(valid_tool_names)


def _voice_live_note(committed, valid_tool_names):
    from tools.voice_live import voice_live_turn_note
    return voice_live_turn_note(committed.get('voice_context') or '')


def _desktop_note(committed, valid_tool_names):
    return DESKTOP_VIEWER_NOTE


_SURFACE_NOTES = {'hud': _hud_note, 'voice-live': _voice_live_note, 'desktop': _desktop_note}


def surface_note(committed, valid_tool_names=None):
    """The model-bound note for a committed surface (``""`` for the plain app window): barge-in
    first, then the HUD read-the-window-below prior or the spoken-delegation contract."""
    notes = []
    if committed.get('interrupted'):
        from tools.tts_streaming import SPEECH_INTERRUPTED_NOTE
        notes.append(SPEECH_INTERRUPTED_NOTE)
    render = _SURFACE_NOTES.get(committed.get('surface'))
    if render is not None:
        notes.append(render(committed, valid_tool_names))
    return '\n\n'.join(note for note in notes if note)


@contextmanager
def surface_turn_scope(committed):
    token = _surface_turn.set(committed)
    try:
        yield
    finally:
        _surface_turn.reset(token)


def surface_voice_turn():
    """Whether the executing admission is a spoken voice turn (``auxiliary.voice_chat`` route)."""
    return bool((_surface_turn.get() or {}).get('voice_turn'))


def surface_turn_client():
    """A copy of the client label the executing admission was stamped with, or ``None``."""
    client = (_surface_turn.get() or {}).get('client')
    return dict(client) if client is not None else None


def surface_turn_note(agent):
    """The executing admission's surface note, gated on the tools this agent actually has."""
    committed = _surface_turn.get()
    if not committed:
        return ''
    return surface_note(committed, getattr(agent, 'valid_tool_names', None))


def arm_surface_turn(agent, notes=(), voice_turn=False):
    """Project the executing admission's surface onto the agent's one-turn inputs, the same way in
    process and in a managed worker: the ``auxiliary.voice_chat`` marker and the must-deliver notes
    that ride the current user message (never the system prompt). Assigned unconditionally so a
    reused agent never replays a stale note or route."""
    agent._voice_turn_pending = bool(voice_turn) or surface_voice_turn()
    agent._gateway_turn_context_notes = '\n\n'.join(
        note for note in (*notes, surface_turn_note(agent)) if note)
