"""Tests for daemon/cost_guard.py.

Pure-function module; no mocking required.
"""
from __future__ import annotations

import pytest

from daemon.cost_guard import (
    COST_OK,
    COST_OVER_HARD,
    COST_OVER_SOFT,
    CostVerdict,
    cost_cents,
    evaluate_cost,
    format_cents,
)


# ---- cost_cents -----------------------------------------------------------


def test_cost_cents_haiku_45_known_rates() -> None:
    """Haiku 4.5: $0.80/MTok in, $4.00/MTok out (per Anthropic public pricing).
    1M input tokens should cost 80 cents; 1M output tokens 400 cents."""
    assert cost_cents(
        model="claude-haiku-4-5",
        input_tokens=1_000_000,
        output_tokens=0,
    ) == pytest.approx(80.0)
    assert cost_cents(
        model="claude-haiku-4-5",
        input_tokens=0,
        output_tokens=1_000_000,
    ) == pytest.approx(400.0)


def test_cost_cents_haiku_45_typical_call() -> None:
    """Realistic call: 2400 input + 200 output tokens on Haiku 4.5.
    in:  2400 / 1M * 80c = 0.192c
    out: 200  / 1M * 400c = 0.08c
    Total ~= 0.272c ($0.0027)"""
    cost = cost_cents(
        model="claude-haiku-4-5",
        input_tokens=2400,
        output_tokens=200,
    )
    assert cost == pytest.approx(0.272, abs=0.001)


def test_cost_cents_zero_tokens() -> None:
    assert cost_cents(
        model="claude-haiku-4-5",
        input_tokens=0,
        output_tokens=0,
    ) == 0.0


def test_cost_cents_unknown_model_uses_fallback() -> None:
    """An unrecognised model name resolves to the conservative
    Sonnet-tier fallback ($3 in / $15 out per MTok), so budget
    calculations stay sensible without crashing."""
    cost = cost_cents(
        model="claude-some-future-model",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert cost == pytest.approx(300.0)


def test_cost_cents_sonnet_46() -> None:
    """Sonnet 4.6 priced at $3 in / $15 out per MTok."""
    cost = cost_cents(
        model="claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == pytest.approx(300.0 + 1500.0)


def test_cost_cents_opus_47() -> None:
    """Opus 4.7 priced at $15 in / $75 out per MTok."""
    cost = cost_cents(
        model="claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == pytest.approx(1500.0 + 7500.0)


# ---- evaluate_cost --------------------------------------------------------


def test_evaluate_under_soft_cap_is_ok() -> None:
    """Realistic Haiku call (~0.27 cents) is well under a 10-cent cap."""
    verdict = evaluate_cost(
        model="claude-haiku-4-5",
        input_tokens=2400,
        output_tokens=200,
        soft_cap_cents=10,
        hard_kill_multiplier=5,
    )
    assert verdict.verdict == COST_OK
    assert verdict.cost_cents_value < 10


def test_evaluate_over_soft_under_hard() -> None:
    """A call between soft cap and hard cap is flagged as over_soft."""
    # Haiku rates: 1M output = 400c. 2 cents soft, 6 cents hard (3x).
    # 50,000 output tokens = 20 cents, which is between soft and hard.
    verdict = evaluate_cost(
        model="claude-haiku-4-5",
        input_tokens=0,
        output_tokens=50_000,
        soft_cap_cents=2,
        hard_kill_multiplier=15,  # hard cap = 30c, cost = 20c
    )
    assert verdict.verdict == COST_OVER_SOFT
    assert verdict.cost_cents_value == pytest.approx(20.0)
    assert verdict.soft_cap_cents == 2
    assert verdict.hard_cap_cents == 30


def test_evaluate_at_or_above_hard_is_killed() -> None:
    """A call at the hard cap is over_hard. Boundary >= , not >."""
    # Cost = 100c, soft cap 10c, hard multiplier 10 => hard cap = 100c.
    verdict = evaluate_cost(
        model="claude-haiku-4-5",
        input_tokens=0,
        output_tokens=250_000,  # 250k * 400c/M = 100c
        soft_cap_cents=10,
        hard_kill_multiplier=10,
    )
    assert verdict.verdict == COST_OVER_HARD
    assert verdict.cost_cents_value == pytest.approx(100.0)
    assert verdict.hard_cap_cents == 100


def test_evaluate_multiplier_one_collapses_bands() -> None:
    """Multiplier 1: any overage is hard-killed, no soft band."""
    # Cost just above the soft cap.
    verdict = evaluate_cost(
        model="claude-haiku-4-5",
        input_tokens=0,
        output_tokens=30_000,  # 12c
        soft_cap_cents=10,
        hard_kill_multiplier=1,  # hard cap also 10c
    )
    assert verdict.verdict == COST_OVER_HARD


def test_evaluate_returns_actual_caps() -> None:
    """The CostVerdict captures the caps in effect at check time."""
    verdict = evaluate_cost(
        model="claude-haiku-4-5",
        input_tokens=100, output_tokens=100,
        soft_cap_cents=25,
        hard_kill_multiplier=4,
    )
    assert verdict.soft_cap_cents == 25
    assert verdict.hard_cap_cents == 100


def test_evaluate_unknown_model_uses_fallback_in_classification() -> None:
    """The fallback rate applies to budget classification too. With the
    Sonnet-tier fallback (300/1500 cents per MTok), a call with 1M
    input tokens costs 300c — over a 10c soft, hard-killed (50c hard)."""
    verdict = evaluate_cost(
        model="claude-mystery-model",
        input_tokens=1_000_000,
        output_tokens=0,
        soft_cap_cents=10,
        hard_kill_multiplier=5,
    )
    assert verdict.verdict == COST_OVER_HARD


# ---- format_cents ---------------------------------------------------------


@pytest.mark.parametrize("cents,expected", [
    (0.0, "$0.00"),
    (0.5, "$0.01"),  # rounding up
    (1.0, "$0.01"),
    (10.0, "$0.10"),
    (100.0, "$1.00"),
    (999.99, "$10.00"),
    (12.345, "$0.12"),
])
def test_format_cents(cents: float, expected: str) -> None:
    assert format_cents(cents) == expected


def test_cost_verdict_is_frozen() -> None:
    """Defensive: the verdict dataclass should be immutable so it can
    be passed around without callers accidentally mutating fields."""
    v = CostVerdict(
        verdict=COST_OK,
        cost_cents_value=1.0,
        soft_cap_cents=10,
        hard_cap_cents=50,
    )
    with pytest.raises((AttributeError, Exception)):
        v.verdict = COST_OVER_HARD  # type: ignore[misc]
