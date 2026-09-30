"""The playbook is the source of truth; these tests protect it.

Everything downstream is derived from `faults.yaml`, so a mistake there shows
up as a mysterious accuracy drop several layers away. Testing the playbook
directly means the failure surfaces where it was introduced.
"""

from __future__ import annotations

import pytest

from ailab_ops.datagen.taxonomy import INSUFFICIENT_EVIDENCE, load_playbook


def test_playbook_loads(playbook):
    assert len(playbook.scenarios) >= 20, "the playbook should cover a realistic range of failures"
    assert playbook.schema_version >= 1
    assert playbook.domain


def test_every_scenario_is_internally_complete(playbook):
    for s in playbook.scenarios.values():
        assert s.name, f"{s.id} has no name"
        assert s.category, f"{s.id} has no category"
        assert s.difficulty in {"easy", "medium", "hard"}, f"{s.id} has a bad difficulty"
        assert s.remediation.strip(), f"{s.id} has no remediation"
        assert s.runbook_body.strip(), f"{s.id} has an empty runbook body"
        assert len(s.runbook_body) > 120, f"{s.id}'s runbook is too thin to be useful for retrieval"


def test_scenario_ids_are_unique(playbook):
    ids = [s.id for s in playbook.scenarios.values()]
    assert len(ids) == len(set(ids))


def test_distractors_reference_real_scenarios(playbook):
    for s in playbook.scenarios.values():
        for d in s.distractors:
            assert d in playbook.scenarios, f"{s.id} names unknown distractor {d}"
            assert d != s.id, f"{s.id} distracts itself"


def test_diagnosable_scenarios_have_a_signature(playbook):
    """A scenario the agent is expected to identify needs something to identify
    it by. A non-unknown scenario with no signals at all would be unanswerable
    while still being scored as answerable."""
    for s in playbook.scenarios.values():
        if s.is_unknown:
            continue
        assert s.signature > 0.3, (
            f"{s.id} has almost no evidence weight ({s.signature:.2f}); either give it a "
            f"signature or mark it as an insufficient-evidence case"
        )


def test_insufficient_evidence_has_no_signature(playbook):
    """The converse: the undecidable case must NOT have a signature, or it would
    simply be a diagnosable failure and the calibration metric would be
    measuring nothing."""
    s = playbook.get(INSUFFICIENT_EVIDENCE)
    assert s.is_unknown
    assert max((p.weight for p in s.log_patterns), default=0.0) == 0.0
    assert not s.metric_shapes


def test_no_single_log_pattern_dominates_the_playbook(playbook):
    """A pattern shared by many scenarios is not a signature.

    `exit code 137` and `Out of memory` are different in kind: the first
    appears in a dozen scenarios and can never identify one. This test catches
    the mistake of adding a popular generic string to a new scenario's
    evidence, which silently makes the whole scoring noisier.
    """
    from collections import Counter

    counts = Counter()
    for s in playbook.scenarios.values():
        for p in s.log_patterns:
            if p.weight > 0:
                counts[p.pattern.lower()] += 1

    offenders = {p: n for p, n in counts.items() if n >= 4}
    assert not offenders, f"log patterns shared by 4+ scenarios (not signatures): {offenders}"


def test_split_ratios_are_sane(playbook):
    total = sum(playbook.split_ratios.values())
    assert 0.95 <= total <= 1.05, f"difficulty split ratios sum to {total}, not ~1"
    assert 0 < playbook.insufficient_evidence_ratio < 0.25, (
        "the undecidable share must be large enough to measure calibration and small enough "
        "that refusing everything is not a winning strategy"
    )


def test_exit_codes_are_plausible(playbook):
    for s in playbook.scenarios.values():
        for c in s.exit_codes:
            assert 0 <= c <= 255, f"{s.id} claims exit code {c}, which is out of range"
