"""
╔══════════════════════════════════════════════════════════╗
║         CLAUDEBOT v4 — Polymarket Paper Trader           ║
║         Short-term only (≤7 days) + Web Research         ║
║                                                          ║
║  SETUP:  pip install anthropic requests                  ║
║  RUN:    python claudebot.py --single-scan               ║
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

# ─────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
PAPER_TRADING      = True
STARTING_BANKROLL  = 1000.00
MAX_BET_PCT        = 5.0        # max % of bankroll per trade
MIN_CONFIDENCE     = 58         # minimum Claude confidence % to place a trade
MIN_EDGE_PCT       = 7          # minimum edge (true_prob - market_prob) to trade
MAX_OPEN_POSITIONS = 5          # max simultaneous open bets
MAX_HOLD_DAYS      = 7          # only bet on markets closing within this many days
MIN_HOLD_HOURS     = 2          # skip markets closing in less than 2 hours
DAILY_LOSS_LIMIT   = 150.00     # stop trading for the day if losses hit this
SCAN_INTERVAL_MINS = 30         # loop interval (used in continuous mode)
LOG_FILE           = "claudebot_log.json"


# ─────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ─────────────────────────────────────────────────────────
#  STATE  ·  persisted to claudebot_log.json in the repo
# ─────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            s = json.load(f)
        log(f"📂 Loaded state — {len(s.get('trades', []))} trades | bankroll ${s.get('bankroll', STARTING_BANKROLL):.2f}")
        return s
    log("📂 No log found — starting fresh")
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
    log(f"💾 Saved — bankroll ${state['bankroll']:.2f} | {len(state['trades'])} trades")


def reset_daily_loss(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_loss"] = 0.0
        state["daily_reset"] = today
        log("📅 New day — daily loss reset")
    return state


# ─────────────────────────────────────────────────────────
#  DATE HELPERS
# ─────────────────────────────────────────────────────────

def parse_utc(date_str):
    """
    Parse any ISO date string into a UTC-aware datetime.
    Returns None if it can't be parsed.
    """
    if not date_str:
        return None
    try:
        clean = date_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def days_until(dt):
    """Return float days from now until dt. Negative = already passed."""
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


# ─────────────────────────────────────────────────────────
#  MARKET RESOLUTION
#  Called every scan to check if open bets have settled
# ─────────────────────────────────────────────────────────

def resolve_open_trades(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return state

    log(f"🔍 Checking {len(open_trades)} open position(s) for resolution...")

    for trade in open_trades:
        market_id = trade.get("market_id", "")

        # ── Real Polymarket market ─────────────────────
        if market_id and not market_id.startswith("d0"):
            try:
                r = requests.get(
                    f"https://gamma-api.polymarket.com/markets/{market_id}",
                    timeout=10
                )
                if r.status_code != 200:
                    continue
                mkt = r.json()

                if mkt.get("active", True) and not mkt.get("closed", False):
                    continue  # still running

                prices = json.loads(mkt.get("outcomePrices", "[0.5,0.5]"))
                yes_price = float(prices[0])
                no_price  = float(prices[1])
                won = (yes_price >= 0.99) if trade["position"] == "YES" else (no_price >= 0.99)
                _settle_trade(trade, won, state)

            except Exception as e:
                log(f"  ⚠️  Could not check {market_id}: {e}")
                continue

        # ── Demo market — settle when past close date ──
        else:
            close_dt = parse_utc(trade.get("closes"))
            if close_dt and datetime.now(timezone.utc) > close_dt:
                import random
                _settle_trade(trade, random.random() > 0.5, state)

    return state


def _settle_trade(trade, won, state):
    trade["status"]      = "closed"
    trade["won"]         = won
    trade["resolved_at"] = datetime.now(timezone.utc).isoformat()

    if won:
        payout              = trade.get("potential_return", trade["stake"])
        trade["realized_pnl"] = round(payout - trade["stake"], 2)
        state["bankroll"]   = round(state["bankroll"] + payout, 2)
        log(f"  ✅ WON  +${trade['realized_pnl']:.2f}  |  {trade['market'][:60]}")
        log(f"         Bankroll now: ${state['bankroll']:.2f}")
    else:
        trade["realized_pnl"] = round(-trade["stake"], 2)
        state["daily_loss"]   = round(state.get("daily_loss", 0) + trade["stake"], 2)
        log(f"  ❌ LOST -${trade['stake']:.2f}  |  {trade['market'][:60]}")
        log(f"         Bankroll now: ${state['bankroll']:.2f}")


# ─────────────────────────────────────────────────────────
#  MARKET FETCHING  ·  short-term filter applied here
# ─────────────────────────────────────────────────────────

DEMO_MARKETS = []  # populated dynamically in get_demo_markets()


def get_demo_markets():
    now = datetime.now(timezone.utc)
    return [
        {"id": "d001", "question": "Will Bitcoin close above $85,000 today?",             "yes": 52, "volume": 1200000, "closes_in_days": 0.5,  "closes": (now + timedelta(hours=12)).isoformat()},
        {"id": "d002", "question": "Will the S&P 500 close up on Friday?",                "yes": 48, "volume":  890000, "closes_in_days": 2.0,  "closes": (now + timedelta(days=2)).isoformat()},
        {"id": "d003", "question": "Will Ethereum be above $2,000 by end of week?",       "yes": 61, "volume":  740000, "closes_in_days": 4.0,  "closes": (now + timedelta(days=4)).isoformat()},
        {"id": "d004", "question": "Will the Fed make any emergency statement this week?", "yes":  8, "volume":  430000, "closes_in_days": 5.0,  "closes": (now + timedelta(days=5)).isoformat()},
        {"id": "d005", "question": "Will BTC dominance exceed 55% by end of week?",       "yes": 44, "volume":  320000, "closes_in_days": 6.0,  "closes": (now + timedelta(days=6)).isoformat()},
        {"id": "d006", "question": "Will there be a major crypto exchange hack this week?","yes":  6, "volume":  210000, "closes_in_days": 7.0,  "closes": (now + timedelta(days=7)).isoformat()},
    ]


def fetch_markets():
    """
    Pull Polymarket markets and keep ONLY those closing within
    MAX_HOLD_DAYS days and at least MIN_HOLD_HOURS from now.
    Every returned market is guaranteed to have a real closes_in_days float.
    """
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets"
            "?active=true&closed=false&limit=100&order=volume&ascending=false",
            timeout=12
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log(f"⚠️  Polymarket unavailable ({e}) — using demo markets")
        return get_demo_markets()

    now      = datetime.now(timezone.utc)
    markets  = []
    skipped  = 0

    for m in raw:
        # ── Basic validity ─────────────────────────────
        if not m.get("question") or not m.get("outcomePrices"):
            continue

        # ── Parse end date — skip anything unparseable ──
        end_dt = parse_utc(m.get("endDate") or m.get("end_date") or "")
        if end_dt is None:
            skipped += 1
            continue

        cid = (end_dt - now).total_seconds() / 86400  # closes_in_days as a float

        # ── Hard window filter ─────────────────────────
        min_days = MIN_HOLD_HOURS / 24
        if cid < min_days or cid > MAX_HOLD_DAYS:
            skipped += 1
            continue

        # ── Parse prices ───────────────────────────────
        try:
            prices = json.loads(m["outcomePrices"])
            yes = round(float(prices[0]) * 100)
        except Exception:
            continue

        # Skip near-certain markets (no edge to find)
        if yes >= 95 or yes <= 5:
            continue

        markets.append({
            "id":             str(m.get("id", "")),
            "question":       m["question"],
            "yes":            yes,
            "volume":         float(m.get("volume", 0)),
            "category":       (m.get("tags") or [{}])[0].get("label", "general"),
            "closes":         end_dt.isoformat(),   # always tz-aware ISO string
            "closes_in_days": round(cid, 2),         # always a real float
            "clobTokenIds":   m.get("clobTokenIds", []),
        })

    markets.sort(key=lambda x: x["closes_in_days"])
    log(f"✅ {len(markets)} markets within {MAX_HOLD_DAYS}d window (skipped {skipped})")
    return markets


# ─────────────────────────────────────────────────────────
#  KELLY SIZING  ·  half-Kelly with short-term discount
# ─────────────────────────────────────────────────────────

def kelly_size(true_prob_pct, market_prob_pct, bankroll, closes_in_days=7.0):
    if not (0 < market_prob_pct < 100) or not (0 < true_prob_pct < 100):
        return 0.0

    p = true_prob_pct  / 100
    q = 1 - p
    b = (1 - market_prob_pct / 100) / (market_prob_pct / 100)

    if b <= 0:
        return 0.0

    full_kelly = (b * p - q) / b
    half_kelly = full_kelly / 2.0

    # Discount for very short-term (higher variance)
    if   closes_in_days <= 1.0: half_kelly *= 0.65
    elif closes_in_days <= 2.0: half_kelly *= 0.80

    capped = min(max(half_kelly, 0.0), MAX_BET_PCT / 100)
    return round(capped * bankroll, 2)


# ─────────────────────────────────────────────────────────
#  CLAUDE ANALYSIS WITH WEB SEARCH
# ─────────────────────────────────────────────────────────

def analyze_markets(markets, state):
    if not ANTHROPIC_API_KEY:
        log("❌ No API key")
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Markets we already hold — skip
    open_ids       = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    open_questions = {t["market"][:50] for t in state["trades"] if t["status"] == "open"}

    candidates = [
        m for m in markets
        if m["id"] not in open_ids
        and not any(m["question"][:50] in q for q in open_questions)
    ]

    if not candidates:
        log("No new markets to analyze — all open slots filled or no new markets")
        return []

    available = MAX_OPEN_POSITIONS - sum(1 for t in state["trades"] if t["status"] == "open")
    if available <= 0:
        log(f"Max open positions ({MAX_OPEN_POSITIONS}) reached")
        return []

    mkt_list = "\n".join(
        f'- ID:{m["id"]} | Closes in {m["closes_in_days"]:.1f}d ({m["closes"][:10]}) '
        f'| "{m["question"]}" | YES={m["yes"]}¢ NO={100-m["yes"]}¢ | Vol=${m["volume"]:,.0f}'
        for m in candidates[:15]
    )

    open_ctx = ""
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if open_trades:
        open_ctx = "\n\nEXISTING OPEN POSITIONS — do NOT recommend these again:\n"
        open_ctx += "\n".join(
            f'- {t["market"][:80]} | {t["position"]} @ {t["entry_price"]}¢'
            for t in open_trades
        )

    prompt = f"""You are an algorithmic prediction market trader specialising in SHORT-TERM markets.

Today: {datetime.now(timezone.utc).strftime("%A %B %d %Y %H:%M UTC")}
Bankroll: ${state['bankroll']:.2f} | Available slots: {available} | Max bet: {MAX_BET_PCT}%
Minimum edge to trade: {MIN_EDGE_PCT}%
{open_ctx}

ALL markets below close within {MAX_HOLD_DAYS} days:
{mkt_list}

YOUR PROCESS — for each candidate market:
1. USE WEB SEARCH to find current, relevant data:
   - Crypto: current price, 24h % change, recent news, technical levels
   - Economics: latest data releases, central bank statements, analyst consensus
   - Politics: latest polls, news developments, expert forecasts
   - Sports: current form, head-to-head, injury news, recent results
2. Apply BASE RATE thinking — what is the historical frequency of this type of event?
3. Look for CROWD BIAS — fear/greed, overreaction to recent news, anchoring
4. Assess MOMENTUM — is the situation trending toward YES or NO?
5. Only recommend if genuine edge ≥ {MIN_EDGE_PCT}% after research

SIZING RULES:
- Short-term (≤2 days): higher variance, be more conservative with size_pct
- Medium-term (3-7 days): standard sizing
- Never recommend size_pct > {MAX_BET_PCT}

After researching the most promising markets, return ONLY a valid JSON array.
No preamble, no explanation, no markdown fences. Just the JSON:

[
  {{
    "market_id": "exact ID from the list",
    "market": "exact question text",
    "position": "YES or NO",
    "market_prob": 48,
    "true_prob": 63,
    "confidence": 74,
    "size_pct": 3,
    "research_summary": "2-3 sentences: what you found and why it gives you edge",
    "key_factors": ["factor 1", "factor 2", "factor 3"],
    "bear_case": "main reason you could be wrong"
  }}
]

If after thorough research you find no edge ≥ {MIN_EDGE_PCT}%, return exactly: []"""

    log("🔬 Claude researching markets with web search...")

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )

        searches = 0
        full_text = ""
        for block in response.content:
            if hasattr(block, "type"):
                if block.type == "tool_use" and block.name == "web_search":
                    searches += 1
                    log(f"  🔍 Searched: \"{block.input.get('query', '')}\"")
                elif block.type == "text":
                    full_text += block.text

        log(f"  📊 {searches} web search(es) completed")

        # Extract JSON
        raw = full_text.strip().replace("```json", "").replace("```", "").strip()
        if not raw.startswith("["):
            match = re.search(r'\[[\s\S]*\]', raw)
            raw = match.group(0) if match else "[]"

        recs = json.loads(raw)

        # Filter by minimum edge
        valid = []
        for r in recs:
            edge = abs(r.get("true_prob", 0) - r.get("market_prob", 0))
            if edge < MIN_EDGE_PCT:
                log(f"  ⏭  Edge {edge}% too small — skipping: {r.get('market','')[:50]}")
                continue
            # Verify the market_id exists in our fetched list
            if not any(m["id"] == r.get("market_id") for m in markets):
                log(f"  ⚠️  market_id {r.get('market_id')} not in fetched markets — skipping")
                continue
            valid.append(r)

        log(f"🤖 {len(valid)} trade(s) pass research filter")
        for r in valid:
            log(f"  📋 {r['position']} on: {r['market'][:60]}")
            log(f"     Market={r['market_prob']}% → Claude={r['true_prob']}% (edge +{abs(r['true_prob']-r['market_prob'])}%) | conf {r['confidence']}%")
            log(f"     Research: {r.get('research_summary','')[:120]}")
            log(f"     Bear case: {r.get('bear_case','')[:80]}")

        return valid

    except Exception as e:
        log(f"❌ Claude error: {e}")
        return []


# ─────────────────────────────────────────────────────────
#  TRADE EXECUTION
# ─────────────────────────────────────────────────────────

def place_paper_trade(rec, markets, state):
    # ── Confidence gate ────────────────────────────────
    conf = rec.get("confidence", 0)
    if conf < MIN_CONFIDENCE:
        log(f"  ⏭  Confidence {conf}% < {MIN_CONFIDENCE}% — skip")
        return state

    # ── Duplicate check ────────────────────────────────
    if rec.get("market_id") in {t["market_id"] for t in state["trades"] if t["status"] == "open"}:
        log(f"  ⏭  Already open in this market — skip")
        return state

    # ── Position cap ──────────────────────────────────
    if sum(1 for t in state["trades"] if t["status"] == "open") >= MAX_OPEN_POSITIONS:
        log(f"  ⏭  Max positions reached — skip")
        return state

    # ── Daily loss gate ───────────────────────────────
    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit ${DAILY_LOSS_LIMIT} hit — skip")
        return state

    # ── Get market data — source of truth for dates ───
    mkt = next((m for m in markets if m["id"] == rec["market_id"]), None)
    if mkt is None:
        log(f"  ⏭  Market {rec['market_id']} not in fetched list — skip")
        return state

    # Re-compute closes_in_days right now to be safe
    end_dt = parse_utc(mkt["closes"])
    if end_dt is None:
        log(f"  ⏭  Cannot parse close date — skip")
        return state

    cid = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    if cid < (MIN_HOLD_HOURS / 24) or cid > MAX_HOLD_DAYS:
        log(f"  ⏭  Market closes in {cid:.1f}d — outside [{MIN_HOLD_HOURS}h, {MAX_HOLD_DAYS}d] window — skip")
        return state

    # ── Kelly sizing ──────────────────────────────────
    stake = kelly_size(rec["true_prob"], rec["market_prob"], state["bankroll"], cid)
    if stake < 1.00:
        log(f"  ⏭  Stake ${stake:.2f} too small — skip")
        return state

    entry   = rec["market_prob"] if rec["position"] == "YES" else (100 - rec["market_prob"])
    payout  = round(stake * 100 / entry, 2)
    profit  = round(payout - stake, 2)

    trade = {
        "id":               f"T{int(time.time())}",
        "market_id":        mkt["id"],
        "market":           rec["market"],
        "position":         rec["position"],
        "entry_price":      entry,
        "stake":            stake,
        "potential_return": payout,
        "potential_profit": profit,
        "confidence":       conf,
        "true_prob":        rec["true_prob"],
        "market_prob":      rec["market_prob"],
        "closes_in_days":   round(cid, 2),
        "closes":           end_dt.isoformat(),
        "research_summary": rec.get("research_summary", ""),
        "key_factors":      rec.get("key_factors", []),
        "bear_case":        rec.get("bear_case", ""),
        "status":           "open",
        "placed_at":        datetime.now(timezone.utc).isoformat(),
        "paper":            True,
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  ✅ BET PLACED")
    log(f"     {trade['position']} @ {entry}¢  |  closes {end_dt.strftime('%b %d')} ({cid:.1f}d)")
    log(f"     Stake ${stake:.2f}  |  Win ${payout:.2f}  |  Edge +${profit:.2f}")
    log(f"     Confidence {conf}%  |  Bankroll now ${state['bankroll']:.2f}")
    log(f"     {trade['market'][:70]}")

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

    print("\n" + "═" * 62)
    print("  CLAUDEBOT v4")
    print("═" * 62)
    print(f"  Bankroll       ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L   ${realized:+.2f}")
    print(f"  Open           {len(open_t)}")
    print(f"  Closed         {len(closed_t)}  ({len(won_t)}W / {len(lost_t)}L  —  {win_rate:.0f}% win rate)")
    print(f"  Scans run      {state.get('scan_count', 0)}")
    print(f"  Max hold       {MAX_HOLD_DAYS} days")
    print("═" * 62)
    if open_t:
        print("\n  OPEN POSITIONS:")
        for t in open_t:
            close_dt = parse_utc(t.get("closes", ""))
            cid = round(days_until(close_dt), 1) if close_dt else "?"
            closes_str = close_dt.strftime("%b %d") if close_dt else "?"
            print(f"  • {t['position']} | ${t['stake']:.2f} | closes {closes_str} ({cid}d) | {t['market'][:50]}")
    print()


# ─────────────────────────────────────────────────────────
#  MAIN ENTRY POINTS
# ─────────────────────────────────────────────────────────

def single_scan():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v4  ·  Single Scan                            ║")
    print(f"║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  |  Max hold: {MAX_HOLD_DAYS}d  |  Min edge: {MIN_EDGE_PCT}%      ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        sys.exit(1)

    state = load_state()
    state = reset_daily_loss(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    log("── Step 1: Resolve open trades ──────────────────────")
    state = resolve_open_trades(state)

    log("── Step 2: Fetch short-term markets ─────────────────")
    markets = fetch_markets()

    if not markets:
        log(f"No markets closing within {MAX_HOLD_DAYS} days — nothing to do")
        save_state(state)
        print_portfolio(state)
        return

    log("── Step 3: Research + analyze ───────────────────────")
    recs = analyze_markets(markets, state)

    log("── Step 4: Place trades ─────────────────────────────")
    if not recs:
        log("No trades this scan")
    else:
        for rec in recs:
            state = place_paper_trade(rec, markets, state)

    log("── Step 5: Save ─────────────────────────────────────")
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v4  ·  Continuous Mode                        ║")
    print(f"║  Interval: {SCAN_INTERVAL_MINS}min  |  Max hold: {MAX_HOLD_DAYS}d  |  Min edge: {MIN_EDGE_PCT}%        ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY not set")
        return

    while True:
        try:
            single_scan()
            log(f"💤 Sleeping {SCAN_INTERVAL_MINS} min...\n")
            time.sleep(SCAN_INTERVAL_MINS * 60)
        except KeyboardInterrupt:
            log("🛑 Stopped")
            break
        except Exception as e:
            log(f"❌ Unexpected error: {e} — retrying in 60s")
            time.sleep(60)


if __name__ == "__main__":
    if "--single-scan" in sys.argv:
        single_scan()
    else:
        run_loop()
