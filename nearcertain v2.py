"""
╔══════════════════════════════════════════════════════════╗
║         NEARCERTAIN — "Fade the Overconfident"           ║
║                                                          ║
║  Strategy: Markets priced YES 75-95¢ resolve NO          ║
║  significantly more often than implied.                  ║
║  Buy NO mechanically after 3 targeted filters.           ║
║                                                          ║
║  v2 FIXES (vs original):                                 ║
║  1. Sports block — tags API + O/U / vs / BO1/BO3/BO5     ║
║     patterns stop football/esports leaking as "other"    ║
║  2. Weather threshold block — "or higher/lower/above"    ║
║     markets are correctly priced; only exact-temp has    ║
║     statistical edge                                     ║
║  3. Economics cap — earnings beats capped at YES < 85¢   ║
║     Large-cap companies at 90%+ YES are legitimately     ║
║     priced; only trade lower-confidence economics        ║
║                                                          ║
║  SIZING — inverse of NO price (lower NO = bigger stake): ║
║  5-8¢ NO  → 1.50% bankroll  (10-20x payout)             ║
║  8-12¢ NO → 1.20% bankroll  (8-12x payout)              ║
║  12-15¢ → 1.00% bankroll   (6-8x payout)                ║
║  15-20¢ → 0.80% bankroll   (5-6x payout)                ║
║  20-25¢ → 0.60% bankroll   (4-5x payout)                ║
║                                                          ║
║  SETUP:  pip install anthropic requests feedparser       ║
║  RUN:    python nearcertain.py --single-scan             ║
╚══════════════════════════════════════════════════════════╝
"""

import time
import json
import os
import sys
import re
import requests
from datetime import datetime, timezone, timedelta
import anthropic

try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except ImportError:
    FEEDPARSER_AVAILABLE = False


# ─────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID  = os.environ.get("TELEGRAM_CHANNEL_ID", "")
TELEGRAM_PERSONAL_ID = os.environ.get("TELEGRAM_PERSONAL_ID", "")

SCREENER_MODEL     = "claude-haiku-4-5-20251001"
PAPER_TRADING      = True
STARTING_BANKROLL  = 1000.00
LOG_FILE           = "nearcertain_log.json"
SCAN_INTERVAL_MINS = 60

# Entry price window — YES 75-95¢ (NO 5-25¢)
YES_ENTRY_MIN = 75
YES_ENTRY_MAX = 95

# Max hold days and minimum hold time
MAX_HOLD_DAYS  = 14
MIN_HOLD_HOURS = 2

# Daily loss limit
DAILY_LOSS_LIMIT = 150.00

# Min market volume
MIN_VOLUME = 1000

# News lookback
NEWS_LOOKBACK_HOURS = 72

# Stake sizing — inverse of NO price so low-probability wins are worthwhile
# Format: if no_price <= max_no: use this pct of bankroll
STAKE_TABLE = [
    {"max_no":  8, "pct": 1.50},   # 5-8¢ NO  → 10-20x payout
    {"max_no": 12, "pct": 1.20},   # 8-12¢ NO → 8-12x payout
    {"max_no": 15, "pct": 1.00},   # 12-15¢ NO → 6-8x payout
    {"max_no": 20, "pct": 0.80},   # 15-20¢ NO → 5-6x payout
    {"max_no": 25, "pct": 0.60},   # 20-25¢ NO → 4-5x payout
]

# Max deployment of bankroll in open positions
MAX_DEPLOY_PCT = 0.50   # 50% — these are long-odds bets, stay liquid

# Blocked categories (after all filters)
BLOCKED_CATEGORIES = {"sports", "conflict", "crypto"}

# ── FIX 2: Weather threshold keywords ──────────────────────
# Weather markets using these phrases are legitimately priced
# (e.g. "Will Panama City be 31°C or higher?" — it usually is)
# Only exact-degree matches have statistical edge.
WEATHER_THRESHOLD_KEYWORDS = [
    "or higher", "or lower", "or below", "or above",
    "at least", "or more", "no less than",
]

# ── FIX 3: Economics earnings cap ──────────────────────────
# Large-cap earnings beats at YES > 85¢ are usually correctly priced.
# Block these completely — they have negative edge.
EARNINGS_BLOCK_PHRASES = [
    "beat quarterly earnings", "beat earnings", "beat q1", "beat q2",
    "beat q3", "beat q4", "beat its earnings", "miss quarterly earnings",
    "miss earnings",
]

# Hard keyword blocks regardless of price
BLOCK_KEYWORDS = [
    "up or down", "odd or even", "odd/even", "total kills",
    "will there be a", "invasion", "strike on", "attack on",
    "declare war", "natural disaster", "hurricane", "earthquake",
    "tsunami", "explosion", "shooting", "assassination",
]

NEWS_FEEDS = [
    ("Reuters",    "https://feeds.reuters.com/reuters/worldNews"),
    ("BBC",        "https://feeds.bbci.co.uk/news/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
]


# ─────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ─────────────────────────────────────────────────────────
#  CATEGORY HELPER
# ─────────────────────────────────────────────────────────

def get_category(question):
    q = question.lower()
    # Sports — comprehensive keyword set
    if any(k in q for k in [
        "nba", "nfl", "mlb", "nhl", "soccer", "football",
        "tennis", "golf", "match", "fc ", " united",
        "o/u", "over/under", "spread", "rebounds", "assists",
        "esport", "valorant", "counter-strike", "dota", "lol:",
        "bucks", "nets", "knicks", "bulls", "heat", "hawks",
        "sixers", "suns", "nuggets", "warriors", "lakers", "celtics",
        "rockets", "f1", "formula 1", "grand prix", "mls",
        "ufc", "mma", "boxing", "fight", "knockout",
        "blue jays", "brewers", "yankees", "red sox", "cubs",
        "dodgers", "giants", "braves", "mets", "astros",
        " vs ", " vs. ", "bo1", "bo3", "bo5",
        "win the series", "who will win",
    ]):
        return "sports"
    if any(k in q for k in [
        "war", "military", "attack", "strike", "invasion",
        "ceasefire", "conflict", "troops", "missile",
        "hezbollah", "hamas", "houthi", "airstrike",
    ]):
        return "conflict"
    if any(k in q for k in ["temperature", "weather", "rain", "snow", "°c", "°f",
                              "celsius", "fahrenheit", "precipitation"]):
        return "weather"
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "defi"]):
        return "crypto"
    if any(k in q for k in ["president", "election", "senate", "congress", "vote",
                              "trump", "biden", "policy", "tariff", "approval"]):
        return "politics"
    if any(k in q for k in ["fed", "rate", "inflation", "gdp", "jobs", "economy",
                              "ecb", "nasdaq", "s&p", "stock", "earnings", "interest rate",
                              "quarterly", "revenue", "profit"]):
        return "economics"
    return "other"


# ─────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────

def send_telegram(msg, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    target = chat_id or TELEGRAM_CHANNEL_ID
    if not target:
        return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": target, "text": msg, "parse_mode": "HTML"}
        r    = requests.post(url, json=data, timeout=10)
        if r.status_code == 200:
            log(f"📨 Telegram → {target}")
    except Exception as e:
        log(f"⚠️  Telegram failed: {e}")


def should_send_summary(state):
    now       = datetime.now(timezone.utc)
    last_sent = state.get("last_summary_sent", "")
    if not last_sent:
        return True
    try:
        last_dt = datetime.fromisoformat(last_sent)
        return (now - last_dt).total_seconds() >= 12 * 3600
    except Exception:
        return True


def telegram_summary(state):
    if not TELEGRAM_PERSONAL_ID:
        return
    trades   = state["trades"]
    open_t   = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t    = [t for t in closed_t if t.get("won")]
    lost_t   = [t for t in closed_t if not t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed_t)
    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0
    roi      = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    deployed = sum(t["stake"] for t in open_t)

    msg = (
        f"📊 <b>NEARCERTAIN — 12H SUMMARY</b>\n"
        f"{'─' * 28}\n"
        f"🏦 Bankroll: <b>${state['bankroll']:.2f}</b> ({roi:+.1f}% ROI)\n"
        f"💰 Realized P&L: <b>${realized:+.2f}</b>\n"
        f"{'─' * 28}\n"
        f"📋 Open: <b>{len(open_t)}</b> positions (${deployed:.2f} deployed)\n"
        f"{'─' * 28}\n"
        f"📈 Win rate: <b>{win_rate:.0f}%</b> "
        f"({len(won_t)}W / {len(lost_t)}L)\n"
        f"✅ Won: <b>${sum(t.get('realized_pnl',0) for t in won_t):+.2f}</b>\n"
        f"❌ Lost: <b>${sum(t.get('realized_pnl',0) for t in lost_t):.2f}</b>\n"
        f"{'─' * 28}\n"
        f"🔄 Total scans: <b>{state.get('scan_count', 0)}</b>"
    )
    send_telegram(msg, TELEGRAM_PERSONAL_ID)
    log("📨 NearCertain 12h summary sent")


# ─────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            s = json.load(f)
        open_ct = len([t for t in s.get("trades", []) if t["status"] == "open"])
        log(f"📂 Loaded — {len(s.get('trades', []))} trades | "
            f"bankroll ${s.get('bankroll', STARTING_BANKROLL):.2f} | {open_ct} open")
        return s
    log("📂 No log — starting fresh")
    return {
        "bankroll":    STARTING_BANKROLL,
        "trades":      [],
        "daily_loss":  0.0,
        "daily_reset": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scan_count":  0,
        "started":     datetime.now(timezone.utc).isoformat(),
    }


def save_state(state):
    with open(LOG_FILE, "w") as f:
        json.dump(state, f, indent=2)
    open_ct = len([t for t in state["trades"] if t["status"] == "open"])
    log(f"💾 Saved — bankroll ${state['bankroll']:.2f} | {open_ct} open positions")


def reset_daily_loss(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_loss"] = 0.0
        state["daily_reset"] = today
        log("📅 Daily loss reset")
    return state


# ─────────────────────────────────────────────────────────
#  DATE HELPERS
# ─────────────────────────────────────────────────────────

def parse_utc(date_str):
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def days_until(dt):
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


# ─────────────────────────────────────────────────────────
#  MARKET RESOLUTION
# ─────────────────────────────────────────────────────────

def _settle(trade, won, state):
    trade["status"]      = "closed"
    trade["won"]         = won
    trade["resolved_at"] = datetime.now(timezone.utc).isoformat()
    no_price             = trade["entry_no_price"]

    if won:
        payout               = round(trade["stake"] * 100 / no_price, 2)
        trade["realized_pnl"] = round(payout - trade["stake"], 2)
        state["bankroll"]    = round(state["bankroll"] + payout, 2)
        log(f"  ✅ WON   +${trade['realized_pnl']:.2f}  {trade['market'][:55]}")
    else:
        trade["realized_pnl"] = round(-trade["stake"], 2)
        state["daily_loss"]  = round(state.get("daily_loss", 0) + trade["stake"], 2)
        log(f"  ❌ LOST  -${trade['stake']:.2f}  {trade['market'][:55]}")

    log(f"     Bankroll now: ${state['bankroll']:.2f}")


def resolve_open_trades(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return state
    log(f"🔍 Checking {len(open_trades)} open position(s)...")
    for trade in open_trades:
        market_id = trade.get("market_id", "")
        if not market_id or market_id.startswith("d0"):
            continue
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets/{market_id}",
                timeout=10
            )
            if r.status_code != 200:
                continue
            mkt = r.json()

            prices    = json.loads(mkt.get("outcomePrices", "[0.5,0.5]"))
            no_price  = round(float(prices[1]) * 100)

            if mkt.get("active", True) and not mkt.get("closed", False):
                continue

            won = no_price >= 99
            _settle(trade, won, state)

        except Exception as e:
            log(f"  ⚠️  Could not check {market_id}: {e}")
    return state


# ─────────────────────────────────────────────────────────
#  NEWS SCREEN
# ─────────────────────────────────────────────────────────

def get_recent_headlines():
    if not FEEDPARSER_AVAILABLE:
        return []
    headlines = []
    cutoff    = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    for name, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                published = entry.get("published_parsed")
                if published:
                    try:
                        pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                        if pub_dt < cutoff:
                            continue
                    except Exception:
                        pass
                headlines.append(f"[{name}] {entry.get('title', '')}")
        except Exception:
            pass
    return headlines


def haiku_news_screen(markets, headlines):
    if not headlines or not markets:
        return set()

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    mkt_list = "\n".join(
        f'ID:{m["id"]} | "{m["question"]}"'
        for m in markets[:100]
    )
    headline_txt = "\n".join(headlines[:80])

    prompt = (
        f"You are screening prediction markets for a NO-trading bot.\n"
        f"Today: {datetime.now(timezone.utc).strftime('%A %B %d %Y %H:%M UTC')}\n\n"
        f"NEWS HEADLINES (last {NEWS_LOOKBACK_HOURS}h):\n{headline_txt}\n\n"
        f"MARKETS:\n{mkt_list}\n\n"
        f"Return IDs of markets to SKIP because recent news makes YES resolution\n"
        f"significantly more likely than the market price implies. Only flag markets\n"
        f"where there is a DIRECT and SPECIFIC news link.\n"
        f"Be conservative — most markets should NOT be flagged.\n\n"
        f"Return ONLY a JSON array of IDs to skip (empty array if none):\n"
        f'["id1", "id2"]'
    )

    try:
        resp = client.messages.create(
            model=SCREENER_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw   = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[[\s\S]*\]', raw)
        skip  = json.loads(match.group(0) if match else "[]")
        if skip:
            log(f"📰 News screen flagged {len(skip)} market(s) to skip")
        return set(str(s) for s in skip)
    except Exception as e:
        log(f"⚠️  News screen error: {e}")
        return set()


# ─────────────────────────────────────────────────────────
#  MARKET FETCHING — with all 3 fixes applied
# ─────────────────────────────────────────────────────────

def _is_sports_by_tags(market_raw):
    """
    FIX 1a: Use Polymarket's own tags for sports detection.
    Much more reliable than keyword matching alone.
    """
    tags       = market_raw.get("tags") or []
    tag_labels = [t.get("label", "").lower() for t in tags if isinstance(t, dict)]
    tag_slugs  = [t.get("slug",  "").lower() for t in tags if isinstance(t, dict)]
    sports_labels = {
        "nfl", "nba", "mlb", "nhl", "soccer", "tennis", "golf",
        "mma", "ufc", "boxing", "f1", "nascar", "cricket",
        "rugby", "pickleball", "esports", "sports",
    }
    return any(
        "sport" in label or "sport" in slug or label in sports_labels
        for label, slug in zip(tag_labels, tag_slugs)
    )


def _is_sports_by_keywords(question):
    """
    FIX 1b: Extended keyword list catches O/U markets, vs. matches,
    and esports (BO1/BO3/BO5) that slip past tags.
    """
    q = question.lower()
    patterns = [
        r'\bo/u\b', r'\bbo[135]\b',           # O/U 1.5, BO3 etc.
        r' vs\.? ',                             # "X vs Y" or "X vs. Y"
        r'win on \d{4}-\d{2}-\d{2}',          # Polymarket soccer format
    ]
    if any(re.search(p, q) for p in patterns):
        return True
    keywords = [
        "o/u", "over/under", "bo1", "bo3", "bo5", "lol:", "dota 2:",
        "valorant:", "esport", "counter-strike",
    ]
    return any(k in q for k in keywords)


def _is_weather_threshold(question):
    """
    FIX 2: Weather threshold markets ("or higher/lower/above/below") are
    legitimately priced — hot cities really are that hot. Only exact-degree
    temp markets have statistical edge. Return True to BLOCK.
    """
    q = question.lower()
    # Must be a weather market first
    if not any(k in q for k in ["temperature", "°c", "°f", "celsius", "fahrenheit",
                                  "highest temperature", "lowest temperature"]):
        return False
    return any(k in q for k in WEATHER_THRESHOLD_KEYWORDS)


def _is_earnings_at_high_yes(question, yes_price):
    """
    FIX 3: Large-cap earnings beat markets at YES > 85¢ are legitimately priced.
    Block if this looks like an earnings beat question AND YES >= 85¢.
    """
    q = question.lower()
    is_earnings = any(k in q for k in EARNINGS_BLOCK_PHRASES)
    return is_earnings and yes_price >= 85


def fetch_markets():
    MAX_FETCH = 10_000
    raw       = []
    offset    = 0
    limit     = 500

    while len(raw) < MAX_FETCH:
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets"
                f"?active=true&closed=false&limit={limit}&offset={offset}"
                f"&order=volume&ascending=false",
                timeout=12
            )
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            raw += page
            if len(page) < limit:
                break
            offset += limit
        except Exception as e:
            log(f"⚠️  Polymarket fetch error at offset {offset}: {e}")
            break

    log(f"   📄 Fetched {len(raw)} total markets")

    now     = datetime.now(timezone.utc)
    markets = []
    skip_counts = {
        "timing": 0, "price": 0, "volume": 0,
        "sports_tag": 0, "sports_kw": 0, "category": 0,
        "weather_threshold": 0, "earnings_cap": 0,
        "keyword": 0, "duplicate": 0,
    }

    for m in raw:
        if not m.get("question") or not m.get("outcomePrices"):
            continue

        # Timing filter
        end_dt = parse_utc(m.get("endDate") or m.get("end_date") or "")
        if end_dt is None:
            skip_counts["timing"] += 1
            continue
        cid = (end_dt - now).total_seconds() / 86400
        if cid < (MIN_HOLD_HOURS / 24) or cid > MAX_HOLD_DAYS:
            skip_counts["timing"] += 1
            continue

        # Price filter
        try:
            prices    = json.loads(m["outcomePrices"])
            yes_price = round(float(prices[0]) * 100)
            no_price  = round(float(prices[1]) * 100)
        except Exception:
            continue

        if yes_price < YES_ENTRY_MIN or yes_price > YES_ENTRY_MAX:
            skip_counts["price"] += 1
            continue

        # Volume filter
        if float(m.get("volume", 0)) < MIN_VOLUME:
            skip_counts["volume"] += 1
            continue

        question = m["question"]
        q_lower  = question.lower()

        # ── FIX 1: Sports block — tags first, then keywords ──
        if _is_sports_by_tags(m):
            skip_counts["sports_tag"] += 1
            continue
        if _is_sports_by_keywords(question):
            skip_counts["sports_kw"] += 1
            continue

        # Category check
        cat = get_category(question)
        if cat in BLOCKED_CATEGORIES:
            skip_counts["category"] += 1
            continue

        # ── FIX 2: Weather threshold block ───────────────────
        if cat == "weather" and _is_weather_threshold(question):
            skip_counts["weather_threshold"] += 1
            log(f"   🌡️  SKIP (threshold weather): {question[:60]}")
            continue

        # ── FIX 3: Economics earnings cap ────────────────────
        if cat == "economics" and _is_earnings_at_high_yes(question, yes_price):
            skip_counts["earnings_cap"] += 1
            log(f"   💼 SKIP (earnings >85¢ YES): {question[:60]}")
            continue

        # Hard keyword block
        if any(k in q_lower for k in BLOCK_KEYWORDS):
            skip_counts["keyword"] += 1
            continue

        markets.append({
            "id":             str(m.get("id", "")),
            "slug":           m.get("slug", ""),
            "question":       question,
            "yes_price":      yes_price,
            "no_price":       no_price,
            "volume":         float(m.get("volume", 0)),
            "category":       cat,
            "closes":         end_dt.isoformat(),
            "closes_in_days": round(cid, 2),
        })

    markets.sort(key=lambda x: x["volume"], reverse=True)
    log(f"✅ {len(markets)} candidates after filters:")
    log(f"   Skipped — timing:{skip_counts['timing']} | price:{skip_counts['price']} | "
        f"vol:{skip_counts['volume']} | sports_tag:{skip_counts['sports_tag']} | "
        f"sports_kw:{skip_counts['sports_kw']} | weather_threshold:{skip_counts['weather_threshold']} | "
        f"earnings_cap:{skip_counts['earnings_cap']} | category:{skip_counts['category']}")
    return markets


# ─────────────────────────────────────────────────────────
#  STAKE SIZING
# ─────────────────────────────────────────────────────────

def get_stake(no_price, bankroll):
    """
    Inverse sizing: lower NO price = bigger stake so payouts are meaningful.
    All values are % of current bankroll.
    """
    for tier in STAKE_TABLE:
        if no_price <= tier["max_no"]:
            return round(max(bankroll * tier["pct"] / 100, 0.50), 2)
    # Fallback for NO > 25¢ (shouldn't happen given our YES_ENTRY_MAX = 95)
    return round(max(bankroll * 0.50 / 100, 0.50), 2)


# ─────────────────────────────────────────────────────────
#  TRADE EXECUTION
# ─────────────────────────────────────────────────────────

def place_trade(market, state):
    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    if market["id"] in open_ids:
        return state

    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log("  🛑 Daily loss limit — no more trades today")
        return state

    no_price = market["no_price"]
    stake    = get_stake(no_price, state["bankroll"])

    if stake <= 0:
        return state

    payout   = round(stake * 100 / no_price, 2)
    profit   = round(payout - stake, 2)

    trade = {
        "id":              f"NC{int(time.time() * 1000)}",
        "market_id":       market["id"],
        "market":          market["question"],
        "category":        market["category"],
        "position":        "NO",
        "entry_no_price":  no_price,
        "entry_yes_price": market["yes_price"],
        "stake":           stake,
        "potential_profit": profit,
        "potential_payout": payout,
        "closes":          market["closes"],
        "closes_in_days":  market["closes_in_days"],
        "status":          "open",
        "placed_at":       datetime.now(timezone.utc).isoformat(),
        "paper":           PAPER_TRADING,
        "model":           "nearcertain",
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  🔴 NO @ {no_price}¢ (YES={market['yes_price']}¢) | "
        f"${stake:.2f} stake → ${profit:.2f} profit | "
        f"{market['category']} | {market['question'][:50]}")

    return state


# ─────────────────────────────────────────────────────────
#  PORTFOLIO SUMMARY
# ─────────────────────────────────────────────────────────

def print_portfolio(state):
    trades   = state["trades"]
    open_t   = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t    = [t for t in closed_t if t.get("won")]
    lost_t   = [t for t in closed_t if not t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed_t)
    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0
    roi      = (state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100
    deployed = sum(t["stake"] for t in open_t)

    cat_counts = {}
    for t in open_t:
        c = t.get("category", "?")
        cat_counts[c] = cat_counts.get(c, 0) + 1

    print("\n" + "═" * 65)
    print("  NEARCERTAIN  ·  'Fade the Overconfident'  ·  v2 Filters")
    print("═" * 65)
    print(f"  Bankroll       ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L   ${realized:+.2f}")
    print(f"  Deployed       ${deployed:.2f} across {len(open_t)} open positions")
    print(f"  Closed         {len(closed_t)}  "
          f"({len(won_t)}W / {len(lost_t)}L  —  {win_rate:.0f}% win rate)")
    print(f"  Total Scans    {state.get('scan_count', 0)}")
    print("═" * 65)

    if cat_counts:
        print(f"\n  Open by category: {cat_counts}")

    if open_t:
        print(f"\n  OPEN POSITIONS ({len(open_t)}):")
        for t in sorted(open_t, key=lambda x: x.get("closes_in_days", 99))[:20]:
            close_dt   = parse_utc(t.get("closes", ""))
            cid        = round(days_until(close_dt), 1) if close_dt else "?"
            closes_str = close_dt.strftime("%b %d") if close_dt else "?"
            print(f"  🔴 NO@{t['entry_no_price']}¢ | ${t['stake']:.2f} | "
                  f"{closes_str} ({cid}d) | {t['market'][:45]}")
        if len(open_t) > 20:
            print(f"  ... and {len(open_t) - 20} more")
    print()


# ─────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────

def single_scan():
    now = datetime.now(timezone.utc)
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  NEARCERTAIN  ·  Fade the Overconfident  ·  v2 Filters  ║")
    print(f"║  {now.strftime('%Y-%m-%d %H:%M UTC')}  |  YES 75-95¢ window           ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        sys.exit(1)

    state = load_state()
    state = reset_daily_loss(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    if should_send_summary(state):
        telegram_summary(state)
        state["last_summary_sent"] = now.isoformat()

    # ── Step 1: Resolve ───────────────────────────────────
    log("── Step 1: Resolve & check exits ───────────────────────")
    state = resolve_open_trades(state)

    # ── Step 2: Fetch candidates ──────────────────────────
    log("── Step 2: Fetch markets (YES 75-95¢ window) ───────────")
    markets = fetch_markets()

    if not markets:
        log("No candidates this scan")
        save_state(state)
        print_portfolio(state)
        return

    # ── Step 3: News screen ───────────────────────────────
    log("── Step 3: News screen ──────────────────────────────────")
    headlines = get_recent_headlines()
    skip_ids  = haiku_news_screen(markets, headlines) if headlines else set()
    filtered  = [m for m in markets if m["id"] not in skip_ids]
    log(f"   {len(filtered)} markets pass news screen "
        f"({len(markets) - len(filtered)} skipped by news)")

    # ── Step 4: Place trades ──────────────────────────────
    log(f"── Step 4: Place trades ({len(filtered)} candidates) ────────────")
    new_trades  = 0
    open_ids    = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    max_deploy  = state["bankroll"] * MAX_DEPLOY_PCT
    deployed    = sum(t["stake"] for t in state["trades"] if t["status"] == "open")

    for market in filtered:
        if market["id"] in open_ids:
            continue
        if deployed >= max_deploy:
            log(f"   💰 Deploy cap reached (${deployed:.2f} / ${max_deploy:.2f})")
            break
        prev = state["bankroll"]
        state = place_trade(market, state)
        if state["bankroll"] < prev:
            new_trades += 1
            deployed += (prev - state["bankroll"])

    log(f"   {new_trades} new trade(s) placed")

    # ── Step 5: Save ──────────────────────────────────────
    log("── Step 5: Save ─────────────────────────────────────────")
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  NEARCERTAIN  ·  Continuous Mode  ·  Scanning every 60m ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        return

    while True:
        try:
            single_scan()
            log(f"💤 Sleeping {SCAN_INTERVAL_MINS}m...\n")
            time.sleep(SCAN_INTERVAL_MINS * 60)
        except KeyboardInterrupt:
            log("🛑 Stopped")
            break
        except Exception as e:
            log(f"❌ Error: {e} — retrying in 60s")
            time.sleep(60)


if __name__ == "__main__":
    if "--single-scan" in sys.argv:
        single_scan()
    else:
        run_loop()
