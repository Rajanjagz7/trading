/**
 * TradingView API Gateway using @mathieuc/tradingview
 * Supports:
 * - Real-time market quotes and candles (Client)
 * - Multi-timeframe Technical Analysis / Indicators (getTA)
 * - Market and symbol search (searchMarketV3)
 * - Premium account features via SESSION & SIGNATURE tokens
 */
const TradingView = require('@mathieuc/tradingview');

// In-memory cache for fast sub-millisecond responses
const cache = new Map();
const CACHE_TTL = 3000; // 3 seconds

function getCached(key) {
  const item = cache.get(key);
  if (item && (Date.now() - item.time < CACHE_TTL)) {
    return item.data;
  }
  return null;
}

function setCache(key, data) {
  cache.set(key, { time: Date.now(), data });
  if (cache.size > 200) {
    const oldestKey = cache.keys().next().value;
    cache.delete(oldestKey);
  }
}

// Format TradingView symbol if not already prefixed
function formatTvSymbol(raw) {
  if (!raw) return 'NSE:NIFTY';
  let s = String(raw).trim().toUpperCase();
  if (s.includes(':')) return s;
  if (s === 'SENSEX' || s === 'BANKEX') return `BSE:${s}`;
  return `NSE:${s}`;
}

// Convert TA score (-1.0 to +1.0) into human readable signal
function scoreToSignal(score) {
  if (score >= 0.5) return 'STRONG BUY';
  if (score >= 0.1) return 'BUY';
  if (score <= -0.5) return 'STRONG SELL';
  if (score <= -0.1) return 'SELL';
  return 'NEUTRAL';
}

module.exports = async (req, res) => {
  // Enable CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, x-tv-session, x-tv-signature');

  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = url.pathname.replace(/^\/api\/tv\/?/, '');
  const searchParams = url.searchParams;

  const action = pathname || searchParams.get('action') || 'status';
  const symbol = formatTvSymbol(searchParams.get('symbol') || searchParams.get('sym') || 'NSE:NIFTY');

  // Premium credentials from env or request headers
  const sessionToken = req.headers['x-tv-session'] || searchParams.get('session') || process.env.TRADINGVIEW_SESSION || process.env.SESSION;
  const signatureToken = req.headers['x-tv-signature'] || searchParams.get('signature') || process.env.TRADINGVIEW_SIGNATURE || process.env.SIGNATURE;
  const isPremium = Boolean(sessionToken && signatureToken);

  try {
    // 1. Status & Premium Capability
    if (action === 'status' || action === '') {
      return res.status(200).json({
        status: 'online',
        provider: 'TradingView-API (@mathieuc/tradingview)',
        premiumActive: isPremium,
        supportedActions: ['quote', 'ta', 'search', 'status'],
        version: '1.0.0',
        timestamp: new Date().toISOString()
      });
    }

    // 2. Technical Analysis (TA)
    if (action === 'ta') {
      const cacheKey = `ta_${symbol}`;
      const cached = getCached(cacheKey);
      if (cached) return res.status(200).json(cached);

      const ta = await TradingView.getTA(symbol);
      const summary = {};
      for (const tf of ['1', '5', '15', '60', '1D']) {
        if (ta[tf]) {
          summary[tf] = {
            signal: scoreToSignal(ta[tf].All),
            allScore: ta[tf].All,
            maScore: ta[tf].MA,
            otherScore: ta[tf].Other
          };
        }
      }

      const result = {
        symbol,
        ta,
        summary,
        timestamp: new Date().toISOString()
      };
      setCache(cacheKey, result);
      return res.status(200).json(result);
    }

    // 3. Symbol & Market Search
    if (action === 'search') {
      const query = searchParams.get('query') || searchParams.get('q') || 'NIFTY';
      const cacheKey = `search_${query}`;
      const cached = getCached(cacheKey);
      if (cached) return res.status(200).json(cached);

      const results = await TradingView.searchMarketV3(query);
      const cleaned = (results || []).slice(0, 15).map(r => ({
        id: r.id,
        symbol: r.symbol,
        exchange: r.exchange,
        description: r.description,
        type: r.type
      }));

      const result = { query, results: cleaned };
      setCache(cacheKey, result);
      return res.status(200).json(result);
    }

    // 4. Real-time Quote & Candles via TradingView Client
    if (action === 'quote') {
      const cacheKey = `quote_${symbol}`;
      const cached = getCached(cacheKey);
      if (cached) return res.status(200).json(cached);

      const clientOptions = isPremium ? { token: sessionToken, signature: signatureToken } : {};
      const client = new TradingView.Client(clientOptions);
      const chart = new client.Session.Chart();

      const quotePromise = new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          chart.delete();
          client.end();
          resolve(null);
        }, 4500);

        chart.onError((err) => {
          clearTimeout(timeout);
          chart.delete();
          client.end();
          reject(err);
        });

        chart.setMarket(symbol, { timeframe: 'D' });

        chart.onUpdate(() => {
          if (chart.periods && chart.periods[0]) {
            clearTimeout(timeout);
            const p = chart.periods[0];
            const data = {
              symbol,
              name: chart.infos ? chart.infos.description : symbol,
              exchange: chart.infos ? chart.infos.exchange : 'NSE',
              currency: chart.infos ? chart.infos.currency_id : 'INR',
              ltp: p.close,
              open: p.open,
              high: p.max,
              low: p.min,
              close: p.close,
              volume: p.volume,
              time: p.time,
              periods: chart.periods.slice(0, 5),
              premiumUsed: isPremium,
              timestamp: new Date().toISOString()
            };
            chart.delete();
            client.end();
            resolve(data);
          }
        });
      });

      const data = await quotePromise;
      if (data) {
        setCache(cacheKey, data);
        return res.status(200).json(data);
      } else {
        // Graceful fallback to TA if socket timed out
        const ta = await TradingView.getTA(symbol).catch(() => null);
        return res.status(200).json({
          symbol,
          ta,
          note: 'Live candle timeout; technical analysis provided',
          timestamp: new Date().toISOString()
        });
      }
    }

    return res.status(400).json({ error: `Unknown action: ${action}` });
  } catch (err) {
    console.error(`TradingView API error [${action} - ${symbol}]:`, err);
    return res.status(500).json({
      error: err.message || 'TradingView API processing error',
      symbol,
      action
    });
  }
};
