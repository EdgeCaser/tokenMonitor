"""Model → $/Mtok pricing. Confirmed against the claude-api skill on 2026-06-20.

Cache pricing follows the standard Anthropic multipliers:
  - cache write 5m  = 1.25× input
  - cache write 1h  = 2.00× input
  - cache read      = 0.10× input
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass(frozen=True)
class ModelRate:
    """Per-million-token prices in USD for one model."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float

    @classmethod
    def from_input_output(cls, input_: float, output: float) -> "ModelRate":
        return cls(
            input=input_,
            output=output,
            cache_write_5m=input_ * 1.25,
            cache_write_1h=input_ * 2.00,
            cache_read=input_ * 0.10,
        )


_DEFAULTS: dict[str, ModelRate] = {
    "claude-fable-5": ModelRate.from_input_output(10.00, 50.00),
    "claude-opus-4-8": ModelRate.from_input_output(5.00, 25.00),
    "claude-opus-4-7": ModelRate.from_input_output(5.00, 25.00),
    "claude-opus-4-6": ModelRate.from_input_output(5.00, 25.00),
    "claude-opus-4-5": ModelRate.from_input_output(5.00, 25.00),
    "claude-opus-4-1": ModelRate.from_input_output(15.00, 75.00),
    "claude-opus-4-0": ModelRate.from_input_output(15.00, 75.00),
    "claude-sonnet-4-6": ModelRate.from_input_output(3.00, 15.00),
    "claude-sonnet-4-5": ModelRate.from_input_output(3.00, 15.00),
    "claude-sonnet-4-0": ModelRate.from_input_output(3.00, 15.00),
    "claude-haiku-4-5": ModelRate.from_input_output(1.00, 5.00),
    "claude-haiku-4-5-20251001": ModelRate.from_input_output(1.00, 5.00),
}

_SYNTHETIC_RATE = ModelRate(0, 0, 0, 0, 0)
_FALLBACK_RATE = _DEFAULTS["claude-sonnet-4-6"]
_warned_unknown: set[str] = set()


@dataclass(frozen=True)
class CostBreakdown:
    input_usd: float
    output_usd: float
    cache_write_5m_usd: float
    cache_write_1h_usd: float
    cache_read_usd: float

    @property
    def total_usd(self) -> float:
        return (
            self.input_usd
            + self.output_usd
            + self.cache_write_5m_usd
            + self.cache_write_1h_usd
            + self.cache_read_usd
        )


def load_rates(overrides_path: Path | None = None) -> dict[str, ModelRate]:
    rates = dict(_DEFAULTS)
    if overrides_path is None:
        overrides_path = Path(__file__).parent / "pricing.toml"
    if overrides_path.exists():
        with overrides_path.open("rb") as f:
            data = tomllib.load(f)
        for model, fields in data.get("models", {}).items():
            input_ = fields.get("input")
            output = fields.get("output")
            if input_ is None or output is None:
                continue
            rates[model] = ModelRate(
                input=input_,
                output=output,
                cache_write_5m=fields.get("cache_write_5m", input_ * 1.25),
                cache_write_1h=fields.get("cache_write_1h", input_ * 2.00),
                cache_read=fields.get("cache_read", input_ * 0.10),
            )
    return rates


def rate_for(model: str, rates: dict[str, ModelRate] | None = None) -> ModelRate:
    if model == "<synthetic>" or not model:
        return _SYNTHETIC_RATE
    rates = rates if rates is not None else load_rates()
    if model in rates:
        return rates[model]
    if model not in _warned_unknown:
        _warned_unknown.add(model)
        print(
            f"[tokmon] warning: unknown model {model!r}; using Sonnet-tier fallback",
            file=sys.stderr,
        )
    return _FALLBACK_RATE


def cost_for_turn(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m: int,
    cache_write_1h: int,
    cache_read: int,
    rates: dict[str, ModelRate] | None = None,
) -> CostBreakdown:
    r = rate_for(model, rates)
    return CostBreakdown(
        input_usd=input_tokens * r.input / 1_000_000,
        output_usd=output_tokens * r.output / 1_000_000,
        cache_write_5m_usd=cache_write_5m * r.cache_write_5m / 1_000_000,
        cache_write_1h_usd=cache_write_1h * r.cache_write_1h / 1_000_000,
        cache_read_usd=cache_read * r.cache_read / 1_000_000,
    )
