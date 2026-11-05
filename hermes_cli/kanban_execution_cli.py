"""Bounded JSON handoff for the native admission CLI (private controller input)."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys

from hermes_cli.kanban_execution import MAX_SPEC_BYTES


def read_object(source):
    """Read an explicit file or '-' (a private stdin pipe), never a board field.

    The operating service must protect the file/pipe and authenticate callers.
    This parser grants no authority and does not infer approval from a hash.
    """
    if source is None:
        return None
    with (nullcontext(sys.stdin) if source == '-' else Path(source).open(encoding='utf-8')) as stream:
        raw = stream.read(MAX_SPEC_BYTES + 1)
    if len(raw.encode('utf-8')) > MAX_SPEC_BYTES:
        raise ValueError('execution_spec_too_large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('invalid_execution_specs')
    return value
