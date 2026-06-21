"""Model → $/Mtok pricing. Confirmed against the claude-api skill on 2026-06-20.

Cache pricing follows the standard Anthropic multipliers:
  - cache write 5m  = 1.25× input
  - cache write 1h  = 2.00× input
  - cache read      = 0.10× input
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_PRICING_PATH = Path(__file__).parent / "pricing.toml"
DEFAULT_EFFECTIVE_FROM = date(1970, 1, 1)
ANTHROPIC_PRICING_URL = "https://claude.com/pricing"


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


@dataclass(frozen=True)
class ModelRatePeriod:
    """Per-million-token prices for one model over a half-open date range."""

    model: str
    effective_from: date
    effective_to: date | None
    rate: ModelRate
    source_url: str = ANTHROPIC_PRICING_URL
    note: str = ""


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


def _load_overrides(overrides_path: Path | None = None) -> dict:
    path = overrides_path or DEFAULT_PRICING_PATH
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _parse_date(value: object, *, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"{field} must be an ISO date, got {value!r}")


def _rate_from_fields(fields: dict) -> ModelRate | None:
    input_ = fields.get("input")
    output = fields.get("output")
    if input_ is None or output is None:
        return None
    return ModelRate(
        input=input_,
        output=output,
        cache_write_5m=fields.get("cache_write_5m", input_ * 1.25),
        cache_write_1h=fields.get("cache_write_1h", input_ * 2.00),
        cache_read=fields.get("cache_read", input_ * 0.10),
    )


def load_rates(overrides_path: Path | None = None) -> dict[str, ModelRate]:
    """Current effective rate per model.

    Kept for one-off calculations and backwards compatibility. Analytics uses
    `load_rate_periods()` so historical turns can keep historical prices.
    """
    rates = dict(_DEFAULTS)
    data = _load_overrides(overrides_path)
    for model, fields in data.get("models", {}).items():
        rate = _rate_from_fields(fields)
        if rate is not None:
            rates[model] = rate
    price_rows = []
    for period in data.get("prices", []):
        rate = _rate_from_fields(period)
        model = period.get("model")
        if model and rate is not None:
            effective_from = _parse_date(
                period.get("effective_from", DEFAULT_EFFECTIVE_FROM),
                field="effective_from",
            )
            price_rows.append((effective_from, model, rate))
    for _effective_from, model, rate in sorted(price_rows):
        rates[model] = rate
    return rates


def _validate_periods(periods: list[ModelRatePeriod]) -> None:
    by_model: dict[str, list[ModelRatePeriod]] = {}
    for p in periods:
        by_model.setdefault(p.model, []).append(p)
    for model, model_periods in by_model.items():
        ordered = sorted(model_periods, key=lambda p: p.effective_from)
        prev_to: date | None = None
        for p in ordered:
            if p.effective_to is not None and p.effective_to <= p.effective_from:
                raise ValueError(
                    f"pricing period for {model!r} ends before it starts: {p}"
                )
            if prev_to is not None and p.effective_from < prev_to:
                raise ValueError(f"overlapping pricing periods for {model!r}")
            prev_to = p.effective_to


def load_rate_periods(
    overrides_path: Path | None = None,
) -> dict[str, list[ModelRatePeriod]]:
    rates = dict(_DEFAULTS)
    data = _load_overrides(overrides_path)
    for model, fields in data.get("models", {}).items():
        rate = _rate_from_fields(fields)
        if rate is not None:
            rates[model] = rate

    periods: dict[str, list[ModelRatePeriod]] = {
        model: [ModelRatePeriod(model, DEFAULT_EFFECTIVE_FROM, None, rate)]
        for model, rate in rates.items()
    }

    raw_periods = data.get("prices", [])
    if raw_periods:
        replaced_models = {
            p.get("model") for p in raw_periods
            if p.get("model") and _rate_from_fields(p) is not None
        }
        for model in replaced_models:
            periods[model] = []
        for p in raw_periods:
            model = p.get("model")
            rate = _rate_from_fields(p)
            if not model or rate is None:
                continue
            effective_from = _parse_date(
                p.get("effective_from", DEFAULT_EFFECTIVE_FROM),
                field="effective_from",
            )
            effective_to = (
                _parse_date(p["effective_to"], field="effective_to")
                if p.get("effective_to") is not None else None
            )
            periods.setdefault(model, []).append(
                ModelRatePeriod(
                    model=model,
                    effective_from=effective_from,
                    effective_to=effective_to,
                    rate=rate,
                    source_url=p.get("source_url", ANTHROPIC_PRICING_URL),
                    note=p.get("note", ""),
                )
            )

    flat = [p for model_periods in periods.values() for p in model_periods]
    _validate_periods(flat)
    return {
        model: sorted(model_periods, key=lambda p: p.effective_from)
        for model, model_periods in periods.items()
    }


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


def rate_for_at(
    model: str,
    when: date | datetime | None,
    periods: dict[str, list[ModelRatePeriod]] | None = None,
) -> ModelRate:
    if model == "<synthetic>" or not model:
        return _SYNTHETIC_RATE
    day = when.date() if isinstance(when, datetime) else when
    periods = periods if periods is not None else load_rate_periods()
    for period in periods.get(model, []):
        if day is None:
            if period.effective_to is None:
                return period.rate
            continue
        if day >= period.effective_from and (
            period.effective_to is None or day < period.effective_to
        ):
            return period.rate
    return rate_for(model)


def cost_for_turn(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m: int,
    cache_write_1h: int,
    cache_read: int,
    rates: dict[str, ModelRate] | None = None,
    at: date | datetime | None = None,
    periods: dict[str, list[ModelRatePeriod]] | None = None,
) -> CostBreakdown:
    r = rate_for(model, rates) if at is None else rate_for_at(model, at, periods)
    return CostBreakdown(
        input_usd=input_tokens * r.input / 1_000_000,
        output_usd=output_tokens * r.output / 1_000_000,
        cache_write_5m_usd=cache_write_5m * r.cache_write_5m / 1_000_000,
        cache_write_1h_usd=cache_write_1h * r.cache_write_1h / 1_000_000,
        cache_read_usd=cache_read * r.cache_read / 1_000_000,
    )
