"""Per-profile model allowlist for every model picker.

``model_catalog.allowed_models`` maps a provider slug to the model ids the pickers may offer::

    model_catalog:
      allowed_models:
        anthropic: [claude-opus-5-5, claude-sonnet-5-5]
        openrouter: [z-ai/glm-5.3]

When the mapping is set, providers it does not name are dropped from the picker and each named
provider shows exactly its listed models, in that order. An empty or missing mapping changes nothing.
Pickers only: runtime routing (fallbacks, delegation, auxiliary tasks) is untouched.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _norm(slug) -> str:
    s = str(slug or "").strip().lower()
    return s[len("custom:"):] if s.startswith("custom:") else s


def load_allowed_models(cfg: dict | None = None) -> dict[str, list[str]]:
    """``{provider_slug: [model_id, ...]}`` from config; ``{}`` when unset or malformed."""
    if cfg is None:
        try:
            # Raw user config.yaml (cached): the key has no default, and pickers count full loads.
            from hermes_cli.config import read_raw_config_readonly
            cfg = read_raw_config_readonly()
        except Exception as exc:  # a bad config must never break the picker
            logger.debug("model allowlist: config read failed: %s", exc)
            return {}
    raw = ((cfg or {}).get("model_catalog") or {}).get("allowed_models")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for slug, models in raw.items():
        if isinstance(models, str):
            models = [models]
        if not isinstance(models, list):
            continue
        ids = [str(m).strip() for m in models if str(m or "").strip()]
        if ids and _norm(slug):
            out[_norm(slug)] = list(dict.fromkeys(ids))
    return out


def apply_model_allowlist(rows: list, allowed: dict[str, list[str]] | None = None) -> list:
    """Return picker rows narrowed to the allowlist (rows unchanged when it is empty)."""
    if allowed is None:
        allowed = load_allowed_models()
    if not allowed:
        return rows
    kept = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        models = allowed.get(_norm(row.get("slug"))) or allowed.get(_norm(row.get("name")))
        if not models:
            continue
        row = dict(row)
        row["models"] = list(models)
        row["total_models"] = len(models)
        if isinstance(row.get("featured_models"), list):
            row["featured_models"] = [m for m in row["featured_models"] if m in models]
        kept.append(row)
    return kept
