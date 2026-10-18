"""The maintenance prompt must permit justified no-op and standalone skills."""

from agent.curator import CURATOR_REVIEW_PROMPT


def test_review_has_no_forced_archival_or_umbrella_quota():
    prompt = CURATOR_REVIEW_PROMPT.lower()
    for pressure in (
        "fewer than 10 archives",
        "keep' is a legitimate decision only",
        "do not reject consolidation",
        "don't stop after 3",
        "not a passive audit and not a duplicate-finder",
        "expect 10-25 clusters",
    ):
        assert pressure not in prompt, pressure
    assert "zero changes is a valid outcome" in prompt
    assert "no archive, merge or reduction quota" in prompt
    assert "distinct triggers" in prompt
    assert "recently created or actively used" in prompt


def test_review_retains_package_and_authority_guards():
    prompt = CURATOR_REVIEW_PROMPT.lower()
    for guard in (
        "do not touch bundled, hub-installed, or external-dir skills",
        "do not touch skills shown as pinned=yes",
        "read before write",
        "complete directory package",
        "absorbed_into=<umbrella>",
        "## structured summary (required)",
        "consolidations:",
        "prunings:",
    ):
        assert guard in prompt, guard
