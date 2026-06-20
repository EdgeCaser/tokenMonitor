import pytest
from tokmon.pricing import cost_for_turn, rate_for, load_rates


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
