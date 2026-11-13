"""model_catalog.allowed_models narrows every model picker (hermes_cli/model_allowlist.py)."""

from hermes_cli.model_allowlist import apply_model_allowlist, load_allowed_models


def _rows():
    return [
        {"slug": "moa", "name": "Mixture of Agents", "models": ["default"], "total_models": 1},
        {"slug": "anthropic", "name": "Anthropic", "models": ["claude-opus-4-8", "claude-opus-5-5", "claude-haiku-5-5"],
         "total_models": 3, "featured_models": ["claude-opus-5-5", "claude-opus-4-8"]},
        {"slug": "openrouter", "name": "OpenRouter", "models": ["z-ai/glm-5.3", "openai/gpt-6-sol"], "total_models": 2},
        {"slug": "getvibe", "name": "GetVibeAPI", "models": ["a", "b"], "total_models": 2, "is_user_defined": True},
    ]


def test_unset_allowlist_changes_nothing():
    rows = _rows()
    assert load_allowed_models({}) == {}
    assert load_allowed_models({"model_catalog": {"allowed_models": "bad"}}) == {}
    assert apply_model_allowlist(rows, {}) == rows


def test_allowlist_keeps_only_named_providers_and_models():
    allowed = load_allowed_models({"model_catalog": {"allowed_models": {
        "Anthropic": ["claude-opus-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
        "openrouter": "z-ai/glm-5.3",
        "custom:getvibe": ["b"],
        "zai": [],
    }}})
    assert allowed == {"anthropic": ["claude-opus-5-5", "claude-haiku-5-5"],
                       "openrouter": ["z-ai/glm-5.3"], "getvibe": ["b"]}
    out = apply_model_allowlist(_rows(), allowed)
    assert [r["slug"] for r in out] == ["anthropic", "openrouter", "getvibe"]
    assert out[0]["models"] == ["claude-opus-5-5", "claude-haiku-5-5"]
    assert out[0]["total_models"] == 2
    assert out[0]["featured_models"] == ["claude-opus-5-5"]
    assert out[1]["models"] == ["z-ai/glm-5.3"]
    assert out[2]["models"] == ["b"]


def test_inventory_payload_applies_allowlist(monkeypatch):
    import hermes_cli.inventory as inv
    import hermes_cli.model_switch as ms
    from hermes_cli import model_allowlist

    monkeypatch.setattr(ms, "list_authenticated_providers", lambda **kw: _rows())
    monkeypatch.setattr(inv, "_local_runtime_row", lambda ctx: None)
    monkeypatch.setattr(inv, "_moa_provider_row", lambda current: None)
    monkeypatch.setattr(model_allowlist, "load_allowed_models",
                        lambda cfg=None: {"openrouter": ["z-ai/glm-5.3"]})
    ctx = inv.ConfigContext(current_provider="openrouter", current_model="z-ai/glm-5.3", current_base_url="",
                            user_providers={}, custom_providers=[], excluded_providers=[])
    payload = inv.build_models_payload(ctx)
    assert [(r["slug"], r["models"]) for r in payload["providers"]] == [("openrouter", ["z-ai/glm-5.3"])]
