"""Token-cost backstop for principal-interpret calls.

Pure utility: given a model name and token counts, return the cost in
cents and decide whether the call exceeded a configured ceiling.

Why this lives in its own module:

- Triage's existing rate limiter (`per_hour_max_invocations`) caps the
  number of calls but not their individual cost. A pathological email
  body could pass the rate limit and still spend a disproportionate
  share of the daily Anthropic-console budget in one shot.
- The drop-interpret-gate work (#107) removed the up-front 'yes interpret'
  confirmation. That gate was originally a brake against runaway costs.
  Replacing the brake with a post-hoc cost check is the equivalent
  protection without the per-message friction.
- Per-call cost depends on the model (different models have different
  per-token rates). Centralising the price table here keeps the rates
  out of the watcher and easy to update when Anthropic publishes new
  prices.

Prices below are in cents per million tokens. Source: Anthropic public
pricing page (https://www.anthropic.com/pricing). Snapshot date:
2026-05-06. If a model isn't in the table, the daemon falls back to a
conservative default that errs on the high side (so unknown models
trip the cap sooner rather than later — better to over-warn than to
under-warn).
"""
from __future__ import annotations

from dataclasses import dataclass


# Cents per 1,000,000 tokens, by direction.
# Anthropic's published rates as of 2026-05-06.
_PRICES_CENTS_PER_MTOK: dict[str, tuple[int, int]] = {
    # Haiku 4.5: cheapest production model. Default for triage and
    # principal-interpret.
    "claude-haiku-4-5": (80, 400),  # $0.80 in / $4.00 out per MTok
    # Sonnet 4.6: next-tier; not the daemon's default but supported.
    "claude-sonnet-4-6": (300, 1500),  # $3 in / $15 out per MTok
    # Opus 4.7: top-tier. Daemon defaults won't pick this; if an
    # operator overrides, the price still resolves correctly.
    "claude-opus-4-7": (1500, 7500),  # $15 in / $75 out per MTok
}

# Conservative fallback for an unknown model name. Picks Sonnet rates
# (mid-tier) so budget calculations stay sensible even if the operator
# pins the daemon to a model the price table doesn't know about.
_FALLBACK_PRICE_CENTS_PER_MTOK: tuple[int, int] = (300, 1500)


def cost_cents(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Return the dollar cost of one Claude call in cents.

    Returned as a float because cents-per-call is rarely whole; the
    caller compares against an integer ceiling using >= / <=.

    A model name not in `_PRICES_CENTS_PER_MTOK` resolves to the
    conservative fallback.
    """
    in_rate, out_rate = _PRICES_CENTS_PER_MTOK.get(
        model, _FALLBACK_PRICE_CENTS_PER_MTOK
    )
    in_cost = (input_tokens / 1_000_000.0) * in_rate
    out_cost = (output_tokens / 1_000_000.0) * out_rate
    return in_cost + out_cost


# Verdict bands for a per-call cost check. Encoded as an enum-like set
# of string constants so log events render directly without an extra
# .name lookup.
COST_OK = "ok"          # under the soft cap
COST_OVER_SOFT = "over_soft"  # >= soft cap, < hard kill
COST_OVER_HARD = "over_hard"  # >= hard kill (refuse to surface output)


@dataclass(frozen=True)
class CostVerdict:
    """The outcome of one cost-cap check.

    `verdict` is one of COST_OK / COST_OVER_SOFT / COST_OVER_HARD.
    `cost_cents_value` is the actual computed cost (float, cents).
    `soft_cap_cents` and `hard_cap_cents` are the configured thresholds
    at the time of the check, captured here so log lines and reply
    bodies can quote what the limits were without re-deriving them.
    """
    verdict: str
    cost_cents_value: float
    soft_cap_cents: int
    hard_cap_cents: int


def evaluate_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    soft_cap_cents: int,
    hard_kill_multiplier: int,
) -> CostVerdict:
    """Compute the cost of one call and classify it against the caps.

    `soft_cap_cents` is `[claude].principal_per_message_cost_cents`.
    `hard_kill_multiplier` is `[claude].principal_hard_kill_multiplier`.
    Hard cap = soft_cap * multiplier.

    A multiplier of 1 makes soft and hard the same — meaning every
    overage is a hard refusal. We allow that (config validates >= 1)
    so operators on tight budgets can opt into it.
    """
    cost = cost_cents(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    hard_cap = soft_cap_cents * hard_kill_multiplier
    if cost >= hard_cap:
        verdict = COST_OVER_HARD
    elif cost >= soft_cap_cents:
        verdict = COST_OVER_SOFT
    else:
        verdict = COST_OK
    return CostVerdict(
        verdict=verdict,
        cost_cents_value=cost,
        soft_cap_cents=soft_cap_cents,
        hard_cap_cents=hard_cap,
    )


def format_cents(cents: float) -> str:
    """Render a cent value as a $X.XX string for human-facing output.

    Standard two-decimal float formatting via `:.2f`. Sub-cent values
    render as $0.00 in display but the underlying float is still the
    truth-of-record (logs include the unrounded value)."""
    dollars = cents / 100.0
    return f"${dollars:.2f}"
