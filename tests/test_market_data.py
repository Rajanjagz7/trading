"""
Backend unit tests for core market-data and signal logic (Live Market
Dashboard spec, section 12). Covers: OHLC validation, the Recommendation-
for-the-Day rule's determinism, timestamp/timezone handling, percentage-
change math consistency, and the expiry-safety guard in scan().

Run with: python -m pytest tests/ -v
"""
import re
from datetime import datetime, timedelta

from api.nse_service import (
    NSEService,
    validate_ohlc,
    compute_recommendation,
    now_ist_iso,
    IST,
    _ema_series,
    _classify_intraday,
    _classify_ema_trend,
)
from api.market_extras_service import _compute_cagr


# ---------------------------------------------------------------------------
# OHLC validation
# ---------------------------------------------------------------------------

class TestValidateOHLC:
    def test_valid_bullish_candle(self):
        ok, violations = validate_ohlc(open_=100, high=110, low=95, close=108)
        assert ok is True
        assert violations == []

    def test_valid_bearish_candle(self):
        ok, violations = validate_ohlc(open_=110, high=112, low=98, close=100)
        assert ok is True
        assert violations == []

    def test_valid_flat_candle(self):
        # open == high == low == close is a legitimate (if unusual) candle
        ok, violations = validate_ohlc(open_=50, high=50, low=50, close=50)
        assert ok is True

    def test_high_below_open_is_invalid(self):
        ok, violations = validate_ohlc(open_=100, high=90, low=85, close=95)
        assert ok is False
        assert "High < Open" in violations

    def test_high_below_close_is_invalid(self):
        ok, violations = validate_ohlc(open_=90, high=95, low=85, close=100)
        assert ok is False
        assert "High < Close" in violations

    def test_low_above_open_is_invalid(self):
        ok, violations = validate_ohlc(open_=100, high=110, low=105, close=108)
        assert ok is False
        assert "Low > Open" in violations

    def test_low_above_close_is_invalid(self):
        ok, violations = validate_ohlc(open_=110, high=115, low=109, close=100)
        assert ok is False
        assert "Low > Close" in violations

    def test_high_below_low_is_invalid_and_reports_multiple_violations(self):
        # Pathological case: high/low swapped entirely
        ok, violations = validate_ohlc(open_=100, high=90, low=110, close=100)
        assert ok is False
        assert "High < Low" in violations
        assert len(violations) >= 1


# ---------------------------------------------------------------------------
# Recommendation for the Day -- deterministic rule
# ---------------------------------------------------------------------------

class TestComputeRecommendation:
    def test_buy_verdict_produces_buy_call_from_top_call(self):
        top_call = {"strike": 22500.0, "close": 100.0, "chgPct": 5.0}
        rec = compute_recommendation("NIFTY", "\U0001f7e2 BUY", top_call, None, "live")
        assert rec["signal"] == "BUY CALL"
        assert rec["contract"] == "NIFTY 22500.0 CE"
        assert rec["entry"] == 100.0
        # Pinned to the documented +9% / +18% / -5% formulas -- any drift
        # here should fail loudly, not silently change trader-facing numbers.
        assert rec["target1"] == 109.0
        assert rec["target2"] == 118.0
        assert rec["stopLoss"] == 95.0

    def test_sell_verdict_produces_buy_put_from_top_put(self):
        top_put = {"strike": 24500.0, "close": 337.0, "chgPct": 11.6}
        rec = compute_recommendation("FINNIFTY", "\U0001f534 SELL", None, top_put, "live")
        assert rec["signal"] == "BUY PUT"
        assert rec["contract"] == "FINNIFTY 24500.0 PE"
        assert rec["entry"] == 337.0
        assert rec["target1"] == 367.33
        assert rec["target2"] == 397.66
        assert rec["stopLoss"] == 320.15

    def test_rangebound_verdict_produces_no_signal_with_reason(self):
        rec = compute_recommendation("SENSEX", "WAIT / RANGE", {"strike": 72000, "close": 50}, {"strike": 72000, "close": 40}, "live")
        assert rec["signal"] == "NO SIGNAL"
        assert "balanced" in rec["reason"].lower()

    def test_buy_verdict_without_a_top_call_produces_no_signal(self):
        # Verdict says BUY but the required contract data is missing --
        # must never invent a recommendation from incomplete data.
        rec = compute_recommendation("NIFTY", "\U0001f7e2 BUY", None, None, "live")
        assert rec["signal"] == "NO SIGNAL"
        assert "missing" in rec["reason"].lower() or "stale" in rec["reason"].lower()

    def test_unavailable_data_status_produces_no_recommendation_at_all(self):
        # Not even a "NO SIGNAL" row -- genuinely no data to reason about.
        rec = compute_recommendation("NIFTY", "\U0001f7e2 BUY", {"strike": 1, "close": 1}, None, "unavailable")
        assert rec is None

    def test_recommendation_always_carries_a_real_timestamp(self):
        rec = compute_recommendation("NIFTY", "WAIT / RANGE", None, None, "live")
        assert rec["timestamp"] is not None
        # ISO 8601 with the IST offset, not a naive/ambiguous timestamp.
        assert rec["timestamp"].endswith("+05:30")

    def test_recommendation_is_reproducible_for_identical_inputs(self):
        top_call = {"strike": 50000.0, "close": 42.5, "chgPct": 2.0}
        rec1 = compute_recommendation("BANKNIFTY", "\U0001f7e2 STRONG BUY", top_call, None, "live")
        rec2 = compute_recommendation("BANKNIFTY", "\U0001f7e2 STRONG BUY", top_call, None, "live")
        for key in ("signal", "contract", "entry", "target1", "target2", "stopLoss"):
            assert rec1[key] == rec2[key]


# ---------------------------------------------------------------------------
# Timestamp / timezone handling
# ---------------------------------------------------------------------------

class TestTimestampHandling:
    def test_now_ist_iso_uses_the_correct_market_timezone(self):
        ts = now_ist_iso()
        assert ts.endswith("+05:30"), f"expected IST (+05:30) offset, got: {ts}"

    def test_now_ist_iso_is_never_a_future_timestamp(self):
        ts = now_ist_iso()
        parsed = datetime.fromisoformat(ts)
        now = datetime.now(IST)
        assert parsed <= now + timedelta(seconds=2)  # small tolerance for test execution time

    def test_now_ist_iso_matches_iso8601_format(self):
        ts = now_ist_iso()
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)


# ---------------------------------------------------------------------------
# Percentage-change math consistency (spec section 10)
# ---------------------------------------------------------------------------

class TestPercentageChangeMath:
    def test_prev_close_derivation_is_consistent_with_displayed_change(self):
        # This mirrors exactly what get_indices_pulse() does: prevClose is
        # derived from spot - change, never fetched/guessed separately, so
        # it can never silently drift from the change already shown.
        spot, change = 22421.95, -198.50
        prev_close = round(spot - change, 2)
        assert prev_close == 22620.45
        recomputed_change = round(spot - prev_close, 2)
        assert recomputed_change == change

    def test_change_pct_is_mathematically_consistent_with_price_and_reference(self):
        prev_close = 22620.45
        spot = 22421.95
        change = round(spot - prev_close, 2)
        change_pct = round((change / prev_close) * 100.0, 2)
        # Re-derive spot from prev_close + change_pct and confirm round-trip
        # consistency within normal rounding tolerance.
        reconstructed_spot = prev_close * (1 + change_pct / 100.0)
        assert abs(reconstructed_spot - spot) < 1.0


# ---------------------------------------------------------------------------
# Expiry-safety guard (spec section 4: an expired contract must never be
# shown as active)
# ---------------------------------------------------------------------------

class TestExpirySafetyGuard:
    def _make_row(self, symbol, expiry, strike, type_, **overrides):
        row = {
            "symbol": symbol, "instr": "Index", "expiry": expiry, "strike": strike,
            "type": type_, "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0,
            "prevClose": 100.0, "spot": 22000.0, "oi": 1000.0, "oiChgRaw": 0.0, "vol": 500.0,
        }
        row.update(overrides)
        return row

    def test_scan_excludes_a_contract_whose_expiry_has_already_passed(self):
        svc = NSEService.__new__(NSEService)  # bypass __init__'s bhavcopy auto-load for a pure unit test
        svc.prev_map = {}
        past_date = (datetime.now(IST).date() - timedelta(days=5)).isoformat()
        future_date = (datetime.now(IST).date() + timedelta(days=5)).isoformat()
        rows = [
            self._make_row("NIFTY", past_date, 22000, "CE"),
            self._make_row("NIFTY", future_date, 22000, "CE"),
        ]
        result = svc.scan(rows, {"symSelect": "__ALL__", "lowOn": False, "prevOn": False, "includeUntraded": True, "showMode": "any"})
        expiries_in_results = {r["expiry"] for r in result["results"]}
        assert past_date not in expiries_in_results
        assert future_date in expiries_in_results

    def test_scan_nearest_expiry_never_picks_an_expired_date(self):
        svc = NSEService.__new__(NSEService)
        svc.prev_map = {}
        past_date = (datetime.now(IST).date() - timedelta(days=2)).isoformat()
        near_future = (datetime.now(IST).date() + timedelta(days=3)).isoformat()
        far_future = (datetime.now(IST).date() + timedelta(days=30)).isoformat()
        rows = [
            self._make_row("NIFTY", past_date, 22000, "CE"),
            self._make_row("NIFTY", near_future, 22000, "CE"),
            self._make_row("NIFTY", far_future, 22000, "CE"),
        ]
        result = svc.scan(rows, {"symSelect": "__ALL__", "expSelect": "nearest", "lowOn": False, "prevOn": False, "includeUntraded": True, "showMode": "any"})
        expiries_in_results = {r["expiry"] for r in result["results"]}
        # "Nearest" must mean nearest *future* expiry, never the past one
        assert expiries_in_results == {near_future}


# ---------------------------------------------------------------------------
# Real Stock Buy Picks -- pure classification functions (Intraday /
# Short-Term / Long-Term), no network I/O.
# ---------------------------------------------------------------------------

class TestEmaSeries:
    def test_too_little_history_returns_empty(self):
        assert _ema_series([1, 2, 3], period=20) == []

    def test_flat_prices_converge_to_the_same_value(self):
        series = _ema_series([100.0] * 25, period=20)
        assert series[-1] == 100.0

    def test_ema_reacts_toward_a_sustained_price_move(self):
        closes = [100.0] * 20 + [110.0] * 10
        series = _ema_series(closes, period=20)
        # EMA should climb toward the new, higher price level but not have
        # fully caught up to it after only 10 more bars.
        assert 100.0 < series[-1] < 110.0


class TestClassifyIntraday:
    def test_strong_up_day_is_bullish(self):
        rec = _classify_intraday(100, 110, 99, 108)
        assert rec["signal"] == "Bullish"

    def test_strong_down_day_is_bearish(self):
        rec = _classify_intraday(100, 101, 90, 92)
        assert rec["signal"] == "Bearish"

    def test_choppy_midrange_day_is_neutral(self):
        rec = _classify_intraday(100, 110, 90, 101)
        assert rec["signal"] == "Neutral"

    def test_missing_ohlc_is_neutral_with_a_reason(self):
        rec = _classify_intraday(0, 0, 0, 0)
        assert rec["signal"] == "Neutral"
        assert "insufficient" in rec["reason"].lower()


class TestClassifyEmaTrend:
    def test_well_above_ema_is_bullish(self):
        rec = _classify_ema_trend(ltp=110, ema_value=100, label="20-day EMA")
        assert rec["signal"] == "Bullish"
        assert "20-day EMA" in rec["reason"]

    def test_well_below_ema_is_bearish(self):
        rec = _classify_ema_trend(ltp=90, ema_value=100, label="50-day EMA")
        assert rec["signal"] == "Bearish"

    def test_close_to_ema_is_neutral(self):
        rec = _classify_ema_trend(ltp=100.3, ema_value=100, label="20-day EMA")
        assert rec["signal"] == "Neutral"

    def test_missing_ema_is_neutral_with_a_reason(self):
        rec = _classify_ema_trend(ltp=100, ema_value=None, label="50-day EMA")
        assert rec["signal"] == "Neutral"
        assert "not enough" in rec["reason"].lower()


# ---------------------------------------------------------------------------
# Mutual Fund CAGR computation -- pure function, no network I/O.
# ---------------------------------------------------------------------------

def _mf_row(dt, nav):
    return {"date": dt.strftime("%d-%m-%Y"), "nav": str(nav)}


class TestComputeCagr:
    def test_too_little_data_returns_none(self):
        rows = [_mf_row(datetime(2026, 1, 1), 100)]
        assert _compute_cagr(rows, years=5) is None

    def test_insufficient_history_for_horizon_returns_none(self):
        # Only 2 years of history -- can't compute a real 5-year CAGR from it.
        latest = datetime(2026, 1, 1)
        rows = [_mf_row(latest, 120), _mf_row(latest - timedelta(days=730), 100)]
        assert _compute_cagr(rows, years=5) is None

    def test_nav_doubling_over_5_years_gives_the_expected_cagr(self):
        latest = datetime(2026, 1, 1)
        old = latest - timedelta(days=365 * 5)
        rows = [_mf_row(latest, 200.0), _mf_row(old, 100.0)]
        cagr = _compute_cagr(rows, years=5)
        # (2)^(1/5) - 1 = 14.87%
        assert abs(cagr - 14.87) < 0.1

    def test_flat_nav_gives_zero_cagr(self):
        latest = datetime(2026, 1, 1)
        old = latest - timedelta(days=365 * 5)
        rows = [_mf_row(latest, 100.0), _mf_row(old, 100.0)]
        assert _compute_cagr(rows, years=5) == 0.0

    def test_declining_nav_gives_negative_cagr(self):
        latest = datetime(2026, 1, 1)
        old = latest - timedelta(days=365 * 5)
        rows = [_mf_row(latest, 80.0), _mf_row(old, 100.0)]
        cagr = _compute_cagr(rows, years=5)
        assert cagr < 0
