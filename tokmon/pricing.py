"""Model → $/Mtok pricing.

Anthropic rates confirmed against the claude-api skill on 2026-06-20. Cache
pricing follows the standard Anthropic multipliers:
  - cache write 5m  = 1.25× input
  - cache write 1h  = 2.00× input
  - cache read      = 0.10× input

OpenAI rates (Codex) from https://developers.openai.com/api/docs/pricing,
fetched 2026-09-29, standard tier, short context. OpenAI publishes the cached
input rate directly; it lands in the cache_read column. gpt-5.6 and gpt-6
models also bill cache writes at 1.25× input; gpt-5.4/5.5 list none. Codex
input never approaches the >272K long-context tier (its context window is
258K), so only short-context rates are modeled.
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
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/pricing"


@dataclass(frozen=True)
class ModelRate:
    """Per-million-token prices in USD for one model."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float

    @classmethod
    def openai(cls, input_: float, cached: float, output: float,
               cache_write: float | None = None) -> "ModelRate":
        """OpenAI-style rate: explicit cached-input price; cache writes are
        free unless the model lists a write price."""
        write = input_ if cache_write is None else cache_write
        return cls(
            input=input_,
            output=output,
            cache_write_5m=write,
            cache_write_1h=write,
            cache_read=cached,
        )

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
    # 5-series rates from the claude-api skill's model table (cached
    # 2026-09-25). Opus 5.5 and Fable 5.1 list cache reads below the usual
    # 0.1x of input, so those are set explicitly.
    "claude-fable-5-1": ModelRate(10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-opus-5-5": ModelRate(4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-opus-5": ModelRate.from_input_output(5.00, 25.00),
    "claude-sonnet-5-5": ModelRate.from_input_output(2.00, 10.00),
    "claude-sonnet-5": ModelRate.from_input_output(2.00, 10.00),
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
    # --- OpenAI / Codex ---
    "gpt-5.4": ModelRate.openai(2.50, 0.25, 15.00),
    "gpt-5.5": ModelRate.openai(5.00, 0.50, 30.00),
    # Sol's listed rate is promotional "through at least 2026-11-21"; the
    # post-promo price isn't published yet. Add a [[prices]] row when it is.
    "gpt-5.6-sol": ModelRate.openai(4.00, 0.40, 20.00, cache_write=5.00),
    "gpt-5.6-terra": ModelRate.openai(2.00, 0.20, 12.00, cache_write=2.50),
    "gpt-6-astra": ModelRate.openai(10.00, 1.00, 50.00, cache_write=12.50),
    "gpt-6-sol": ModelRate.openai(2.00, 0.20, 10.00, cache_write=2.50),
}

_LUNA_PRE_CUT = ModelRate.openai(1.00, 0.10, 6.00, cache_write=1.25)
_LUNA = ModelRate.openai(0.20, 0.02, 1.20, cache_write=0.25)
_PRICE_CUT_2026_07_30 = date(2026, 7, 30)

# Dated defaults: models whose price (or identity) changed on a known day.
# Same semantics as [[prices]] rows in pricing.toml, which replace these.
_DEFAULT_PERIODS: list[tuple[str, date, date | None, ModelRate, str, str]] = [
    ("gpt-5.6-luna", DEFAULT_EFFECTIVE_FROM, _PRICE_CUT_2026_07_30, _LUNA_PRE_CUT,
     OPENAI_PRICING_URL, "pre-2026-07-30 rate (secondary source: CloudZero)"),
    ("gpt-5.6-luna", _PRICE_CUT_2026_07_30, None, _LUNA,
     OPENAI_PRICING_URL, "2026-07-30 price cut"),
    # codex-auto-review is an alias for the model behind Codex's approval
    # reviewer: gpt-5.4 until OpenAI moved it to Luna on 2026-07-30.
    ("codex-auto-review", DEFAULT_EFFECTIVE_FROM, _PRICE_CUT_2026_07_30,
     _DEFAULTS["gpt-5.4"], OPENAI_PRICING_URL, "alias of gpt-5.4"),
    ("codex-auto-review", _PRICE_CUT_2026_07_30, None, _LUNA,
     OPENAI_PRICING_URL, "alias of gpt-5.6-luna"),
]

CODEX_MODEL_PREFIXES = ("gpt-", "codex-", "o1", "o3", "o4")


def provider_for_model(model: str) -> str:
    """Best guess at which tool produced a model id."""
    m = (model or "").lower()
    return "codex" if m.startswith(CODEX_MODEL_PREFIXES) else "claude"


def source_url_for(model: str) -> str:
    return OPENAI_PRICING_URL if provider_for_model(model) == "codex" else ANTHROPIC_PRICING_URL

_SYNTHETIC_RATE = ModelRate(0, 0, 0, 0, 0)
_FALLBACK_RATE = _DEFAULTS["claude-sonnet-4-6"]
# Unknown Codex models price as the current Codex default, not as Sonnet.
CODEX_FALLBACK_MODEL = "gpt-5.6-sol"
_CODEX_FALLBACK_RATE = _DEFAULTS[CODEX_FALLBACK_MODEL]
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
    for model, _f, effective_to, rate, _u, _n in _DEFAULT_PERIODS:
        if effective_to is None:
            rates[model] = rate
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
        model: [ModelRatePeriod(model, DEFAULT_EFFECTIVE_FROM, None, rate,
                                source_url=source_url_for(model))]
        for model, rate in rates.items()
    }
    overridden = set(data.get("models", {}))
    dated: dict[str, list[ModelRatePeriod]] = {}
    for model, eff_from, eff_to, rate, url, note in _DEFAULT_PERIODS:
        if model in overridden:
            continue
        dated.setdefault(model, []).append(
            ModelRatePeriod(model, eff_from, eff_to, rate, source_url=url, note=note)
        )
    periods.update(dated)

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
                    source_url=p.get("source_url", source_url_for(model)),
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
    codex = provider_for_model(model) == "codex"
    if model not in _warned_unknown:
        _warned_unknown.add(model)
        tier = CODEX_FALLBACK_MODEL if codex else "Sonnet-tier"
        print(
            f"[tokmon] warning: unknown model {model!r}; using {tier} fallback",
            file=sys.stderr,
        )
    return _CODEX_FALLBACK_RATE if codex else _FALLBACK_RATE


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
