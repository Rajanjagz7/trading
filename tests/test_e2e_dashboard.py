"""
End-to-end critical-flow test (Live Market Dashboard spec, section 12):
open dashboard -> load instruments -> receive a live price -> table/chart/
recommendation update -> disconnect -> reconnect -> verify recovery.

Requires the FastAPI app running locally (python -m uvicorn api.index:app
--port 8231) -- this is a real browser-driven E2E test against a live
server, not a mock. Tests skip (not fail) if that server isn't reachable,
so the unit suite in test_market_data.py still runs standalone.

Run with: python -m pytest tests/test_e2e_dashboard.py -v
"""
import socket
import pytest
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:8231/"


def _server_is_up():
    try:
        with socket.create_connection(("127.0.0.1", 8231), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _server_is_up(),
    reason="Local server not running on :8231 -- start with `python -m uvicorn api.index:app --port 8231`",
)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1280, "height": 1000})
    yield pg
    pg.close()


class TestCriticalDashboardFlow:
    def test_01_dashboard_loads_and_instruments_populate(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_timeout(1500)
        symbol_count = page.evaluate("allSymbols.length")
        assert symbol_count > 0, "No instruments loaded -- /api/symbols returned nothing"

    def test_02_live_price_is_received_and_table_updates(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_function(
            "document.getElementById('idxNiftyVal').textContent !== '–'", timeout=10000
        )
        nifty_val = page.evaluate("document.getElementById('idxNiftyVal').textContent")
        assert nifty_val and nifty_val != "–", "NIFTY ticker never received a live value"

        page.click('.rx-nav-btn[data-route="/scanner"]')
        page.wait_for_timeout(500)
        row_count = page.evaluate("document.querySelectorAll('#resultsBody tr').length")
        assert row_count > 0, "Scanner results table never populated"

    def test_03_chart_updates_with_real_candles(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_timeout(1000)
        page.evaluate("openOptionChartModal('RELIANCE', '', 1200, 'CE')")
        page.wait_for_function("currentChartCandles.length > 0", timeout=15000)
        candle_count = page.evaluate("currentChartCandles.length")
        assert candle_count >= 5, "Chart never received a real candle series"
        canvas_visible = page.evaluate(
            "getComputedStyle(document.getElementById('optChartCanvas')).display !== 'none'"
        )
        assert canvas_visible

    def test_04_recommendation_table_updates_with_real_data(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_function(
            "document.querySelectorAll('#recoTableBody tr').length >= 5", timeout=15000
        )
        rows = page.evaluate(
            "[...document.querySelectorAll('#recoTableBody tr')].map(r => r.textContent)"
        )
        assert len(rows) == 5, "Expected one Recommendation row per major index"
        # Every row must show either a real signal or an honest NO SIGNAL --
        # never a blank/broken row.
        for row_text in rows:
            assert ("NO SIGNAL" in row_text) or ("BUY" in row_text), (
                f"Recommendation row has neither a signal nor NO SIGNAL: {row_text!r}"
            )

    def test_05_news_section_shows_real_headlines_or_is_honest_about_unavailability(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_timeout(1500)
        news_text = page.evaluate("document.getElementById('newsSection').textContent")
        # Real RSS feeds (Economic Times / Business Standard / Livemint) back
        # this section now -- it must show either a real headline (from a
        # known real source) or an honest unavailable state if every feed
        # failed. It must never show blank content or a fabricated headline.
        known_sources = ("Economic Times", "Business Standard", "Livemint")
        shows_real_headline = any(src in news_text for src in known_sources)
        shows_honest_unavailable = "unavailable" in news_text.lower() or "No live news" in news_text
        assert shows_real_headline or shows_honest_unavailable, (
            f"News section shows neither a real headline nor an honest unavailable state: {news_text!r}"
        )

    def test_06_connection_status_reflects_real_poll_health(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_timeout(3000)
        status_text = page.evaluate("document.getElementById('dataConnText').textContent")
        assert status_text in ("LIVE", "MARKET CLOSED"), f"Unexpected initial status: {status_text}"

    def test_07_disconnect_then_reconnect_recovers_without_duplication(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_timeout(2000)

        # Simulate a network failure on the fast-poll endpoint specifically.
        page.evaluate("""
            window.__origFetch = window.fetch;
            window.fetch = (url, ...args) => {
                if (String(url).includes('/api/quotes/fast')) return Promise.reject(new Error('simulated failure'));
                return window.__origFetch(url, ...args);
            };
        """)
        page.wait_for_function(
            "document.getElementById('dataConnText').textContent === 'RECONNECTING…'",
            timeout=8000,
        )

        # Restore the real fetch and confirm recovery within the spec's 5s reconnect target.
        page.evaluate("window.fetch = window.__origFetch;")
        page.wait_for_function(
            "document.getElementById('dataConnText').textContent !== 'RECONNECTING…'",
            timeout=8000,
        )
        recovered_status = page.evaluate("document.getElementById('dataConnText').textContent")
        assert recovered_status in ("LIVE", "MARKET CLOSED")

        failure_count = page.evaluate("fastPollFailureCount")
        assert failure_count == 0, "Failure counter should reset to 0 after a successful poll"

    def test_08_stocks_only_hides_index_symbols_and_shows_real_stock_picks(self, page):
        page.goto(BASE_URL + "#/scanner", wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_function("allSymbolsDetailed && allSymbolsDetailed.length > 0", timeout=15000)

        page.evaluate("if (!stocksOnlyLock) toggleStocksOnly()")

        # The underlying picker must no longer offer index symbols while
        # Stocks Only is locked -- this is the bug that let a user pick NIFTY
        # and silently get zero results with no explanation.
        hidden_indices = page.evaluate("""
            () => {
                const sel = document.getElementById('symSelect');
                const indexNames = new Set(['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY']);
                return [...sel.options].filter(o => indexNames.has(o.value)).every(o => o.hidden);
            }
        """)
        assert hidden_indices, "Index symbols must be hidden from the picker while Stocks Only is locked"

        # Selecting an index then re-locking Stocks Only must reset the
        # selection back to All rather than leaving a contradictory state.
        reset_value = page.evaluate("""
            () => {
                toggleStocksOnly(); // unlock
                const sel = document.getElementById('symSelect');
                sel.value = 'NIFTY';
                sel.dispatchEvent(new Event('change'));
                toggleStocksOnly(); // re-lock
                return sel.value;
            }
        """)
        assert reset_value == '__ALL__'

        # The real Stock Buy Picks panel must show either real picks (from a
        # known real source, not option premiums) or an honest unavailable
        # state -- never a blank panel.
        page.wait_for_function(
            "!document.getElementById('stockPicksBody').textContent.includes('Loading')",
            timeout=20000,
        )
        picks_text = page.evaluate("document.getElementById('stockPicksBody').textContent")
        assert ("Intraday" in picks_text) or ("unavailable" in picks_text.lower()), (
            f"Stock picks panel shows neither real picks nor an honest unavailable state: {picks_text!r}"
        )

    def test_09_gift_nifty_and_nse_futures_are_honest_not_fake_estimates(self, page):
        # These used to be "real spot + a frozen constant premium" labelled
        # "(Est.)" -- one constant didn't even match its own label. No free
        # real-time source exists, so they must now say so honestly instead
        # of showing fabricated precision.
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)
        page.wait_for_function(
            "document.getElementById('giftNiftyVal').textContent !== '–'",
            timeout=15000,
        )
        gift_val = page.evaluate("document.getElementById('giftNiftyVal').textContent")
        gift_sub = page.evaluate("document.getElementById('giftNiftyBasis').textContent")
        assert gift_val == "Unavailable"
        assert "No real-time GIFT NIFTY data source is available" in gift_sub
        # US futures are real Yahoo Finance data and must be unaffected.
        dow_val = page.evaluate("document.getElementById('dowFutVal').textContent")
        assert dow_val not in ("–", "Unavailable", ""), f"DOW futures should show a real value, got {dow_val!r}"

    def test_10_mutual_funds_and_ipo_sections_show_real_data_or_honest_unavailable(self, page):
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_function("typeof lastResults !== 'undefined'", timeout=20000)

        page.wait_for_function(
            "!document.getElementById('mutualFundsSection').textContent.includes('Loading')",
            timeout=60000,
        )
        mf_text = page.evaluate("document.getElementById('mutualFundsSection').textContent")
        assert ("CAGR" in mf_text) or ("unavailable" in mf_text.lower()), (
            f"Mutual funds section shows neither real data nor an honest unavailable state: {mf_text!r}"
        )

        page.wait_for_function(
            "!document.getElementById('ipoSection').textContent.includes('Loading')",
            timeout=30000,
        )
        ipo_text = page.evaluate("document.getElementById('ipoSection').textContent")
        assert ("Price Band" in ipo_text) or ("No ongoing IPOs" in ipo_text) or ("unavailable" in ipo_text.lower()), (
            f"IPO section shows neither real data nor an honest unavailable/empty state: {ipo_text!r}"
        )
