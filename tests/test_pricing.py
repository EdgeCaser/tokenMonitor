import pytest
from datetime import date

from tokmon.pricing import cost_for_turn, load_rate_periods, load_rates, rate_for, rate_for_at


def test_sonnet_rates_match_published():
    r = rate_for("claude-sonnet-4-6")
    assert r.input == 3.00
    assert r.output == 15.00
    assert r.cache_read == pytest.approx(0.30)
    assert r.cache_write_5m == 3.75
    assert r.cache_write_1h == 6.00


def test_opus_rates_match_published():
    r = rate_for("claude-opus-4-8")
    assert r.input == 5.00
    assert r.output == 25.00


def test_haiku_rates_match_published():
    r = rate_for("claude-haiku-4-5")
    assert r.input == 1.00
    assert r.output == 5.00


def test_synthetic_is_free():
    b = cost_for_turn("<synthetic>", 1_000_000, 1_000_000, 0, 0, 0)
    assert b.total_usd == 0


def test_unknown_falls_back_to_sonnet():
    r = rate_for("claude-unknown-future-model")
    assert r.input == 3.00


def test_cost_breakdown_arithmetic():
    b = cost_for_turn("claude-opus-4-6",
                      input_tokens=1_000_000,
                      output_tokens=1_000_000,
                      cache_write_5m=1_000_000,
                      cache_write_1h=1_000_000,
                      cache_read=1_000_000)
    assert b.input_usd == 5.00
    assert b.output_usd == 25.00
    assert b.cache_write_5m_usd == 6.25
    assert b.cache_write_1h_usd == 10.00
    assert b.cache_read_usd == 0.50
    assert b.total_usd == 46.75


def test_effective_dated_rate_lookup_from_toml(tmp_path):
    pricing = tmp_path / "pricing.toml"
    pricing.write_text(
        """
[[prices]]
model = "claude-test-model"
effective_from = "2026-01-01"
effective_to = "2026-06-01"
input = 10.0
output = 20.0

[[prices]]
model = "claude-test-model"
effective_from = "2026-06-01"
input = 1.0
output = 2.0
"""
    )

    periods = load_rate_periods(pricing)
    assert rate_for_at("claude-test-model", date(2026, 5, 31), periods).input == 10.0
    assert rate_for_at("claude-test-model", date(2026, 6, 1), periods).input == 1.0
    assert load_rates(pricing)["claude-test-model"].input == 1.0

    old_cost = cost_for_turn(
        "claude-test-model", 1_000_000, 1_000_000, 0, 0, 0,
        at=date(2026, 5, 31), periods=periods,
    )
    new_cost = cost_for_turn(
        "claude-test-model", 1_000_000, 1_000_000, 0, 0, 0,
        at=date(2026, 6, 1), periods=periods,
    )
    assert old_cost.total_usd == 30.0
    assert new_cost.total_usd == 3.0
