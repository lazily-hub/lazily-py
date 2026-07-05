"""Causal receipt projection — pure reducer laws.

The Python counterpart of the Lean ``LazilyFormal.Receipt`` formal model in
``lazily-formal``. Each test mirrors a named theorem (duplicate idempotency,
stale-generation discard, non-terminal recording, first-terminal recording,
distinct-terminal conflict) plus the outcome vocabulary terminal/non-terminal
classification.
"""

from __future__ import annotations

from lazily import (
    CausalReceipt,
    ReceiptApplyResult,
    ReceiptOutcome,
    ReceiptProjection,
)


def _receipt(
    receipt_id: str = "r",
    causation_id: str = "c",
    generation: int = 3,
    outcome: ReceiptOutcome = ReceiptOutcome.OBSERVED,
) -> CausalReceipt:
    return CausalReceipt(
        receipt_id=receipt_id,
        causation_id=causation_id,
        observer="o",
        generation=generation,
        outcome=outcome,
    )


# =================================================================================
# ReceiptOutcome.is_terminal — observed_nonterminal / accepted_nonterminal /
# applied_terminal / rejected_terminal
# =================================================================================


def test_observed_nonterminal() -> None:
    assert not ReceiptOutcome.OBSERVED.is_terminal


def test_accepted_nonterminal() -> None:
    assert not ReceiptOutcome.ACCEPTED.is_terminal


def test_applied_terminal() -> None:
    assert ReceiptOutcome.APPLIED.is_terminal


def test_rejected_terminal() -> None:
    assert ReceiptOutcome.REJECTED.is_terminal


# =================================================================================
# duplicate_receipt_noop — a duplicate receipt_id is a no-op regardless of body
# =================================================================================


def test_duplicate_receipt_noop() -> None:
    proj = ReceiptProjection("c", 3)
    assert proj.apply(_receipt(receipt_id="r", outcome=ReceiptOutcome.OBSERVED)) is (
        ReceiptApplyResult.RECORDED
    )
    # Same receipt_id with a terminal outcome is still a duplicate (no-op).
    duplicate = _receipt(receipt_id="r", outcome=ReceiptOutcome.REJECTED)
    assert proj.apply(duplicate) is ReceiptApplyResult.DUPLICATE
    assert proj.terminal_outcome is None


# =================================================================================
# stale_generation_discarded — receipts outside the current generation are
# ignored by the current projection
# =================================================================================


def test_stale_generation_discarded() -> None:
    proj = ReceiptProjection("c", 3)
    stale = _receipt(receipt_id="r2", generation=2, outcome=ReceiptOutcome.APPLIED)
    assert proj.apply(stale) is ReceiptApplyResult.STALE_GENERATION
    assert proj.terminal_outcome is None
    assert proj.stale_receipt_ids() == ["r2"]


def test_newer_generation_is_also_stale() -> None:
    """The authority rejects any generation that is not the current one —
    neither older nor newer generations affect the projection."""
    proj = ReceiptProjection("c", 3)
    newer = _receipt(receipt_id="r3", generation=4, outcome=ReceiptOutcome.APPLIED)
    assert proj.apply(newer) is ReceiptApplyResult.STALE_GENERATION
    assert proj.terminal_outcome is None


# =================================================================================
# nonterminal_records_without_terminal_conflict
# =================================================================================


def test_nonterminal_records_without_terminal_conflict() -> None:
    proj = ReceiptProjection("c", 3)
    nt = _receipt(receipt_id="r4", outcome=ReceiptOutcome.ACCEPTED)
    assert proj.apply(nt) is ReceiptApplyResult.RECORDED
    assert proj.terminal_outcome is None
    assert proj.nonterminal_outcomes() == [ReceiptOutcome.ACCEPTED]


def test_nonterminal_does_not_conflict_with_existing_terminal() -> None:
    proj = ReceiptProjection("c", 3)
    proj.apply(_receipt(receipt_id="term", outcome=ReceiptOutcome.APPLIED))
    nt = _receipt(receipt_id="nt", outcome=ReceiptOutcome.OBSERVED)
    assert proj.apply(nt) is ReceiptApplyResult.RECORDED
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED


# =================================================================================
# first_terminal_records — a terminal receipt records when no terminal exists
# =================================================================================


def test_first_terminal_records() -> None:
    proj = ReceiptProjection("c", 3)
    term = _receipt(receipt_id="r5", outcome=ReceiptOutcome.APPLIED)
    assert proj.apply(term) is ReceiptApplyResult.RECORDED
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED
    assert proj.is_terminal


# =================================================================================
# distinct_terminal_conflicts — a second DIFFERENT terminal outcome fails closed
# =================================================================================


def test_distinct_terminal_conflicts() -> None:
    proj = ReceiptProjection("c", 3)
    proj.apply(_receipt(receipt_id="first", outcome=ReceiptOutcome.APPLIED))
    conflicting = _receipt(receipt_id="second", outcome=ReceiptOutcome.REJECTED)
    assert proj.apply(conflicting) is ReceiptApplyResult.TERMINAL_CONFLICT
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED  # unchanged — no winner
    assert proj.in_conflict
    assert proj.conflicting_receipt_ids() == ["second"]


def test_same_terminal_outcome_is_not_a_conflict() -> None:
    """Two terminal receipts with the SAME outcome are both recorded — only a
    DIFFERENT outcome is a conflict."""
    proj = ReceiptProjection("c", 3)
    proj.apply(_receipt(receipt_id="first", outcome=ReceiptOutcome.REJECTED))
    same = _receipt(receipt_id="second", outcome=ReceiptOutcome.REJECTED)
    assert proj.apply(same) is ReceiptApplyResult.RECORDED
    assert not proj.in_conflict


# =================================================================================
# from_receipts — frame-replay authority (max generation seen)
# =================================================================================


def test_from_receipts_defaults_authority_to_max_generation() -> None:
    proj = ReceiptProjection.from_receipts(
        "c",
        [
            _receipt(receipt_id="r1", generation=2, outcome=ReceiptOutcome.APPLIED),
            _receipt(receipt_id="r2", generation=5, outcome=ReceiptOutcome.OBSERVED),
            _receipt(receipt_id="r3", generation=5, outcome=ReceiptOutcome.APPLIED),
        ],
    )
    assert proj.current_generation == 5
    # The generation-2 receipt is stale under the max-generation authority.
    assert proj.stale_receipt_ids() == ["r1"]
    assert proj.terminal_outcome is ReceiptOutcome.APPLIED


def test_from_receipts_with_explicit_authority_overrides_default() -> None:
    proj = ReceiptProjection.from_receipts(
        "c",
        [_receipt(receipt_id="r1", generation=5, outcome=ReceiptOutcome.APPLIED)],
        current_generation=4,
    )
    assert proj.current_generation == 4
    assert proj.stale_receipt_ids() == ["r1"]
    assert proj.terminal_outcome is None
