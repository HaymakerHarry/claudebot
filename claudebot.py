"""
╔══════════════════════════════════════════════════════╗
║           CLAUDEBOT — Polymarket Paper Trader        ║
║           v2 — with persistent memory & resolution   ║
╚══════════════════════════════════════════════════════╝
"""

import time
import json
import os
import sys
import requests
from datetime import datetime
import anthropic

# ─────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
PAPER_TRADING       = True
STARTING_BANKROLL   = 1000.00
MAX_BET_PCT         = 5.0
MIN_CONFIDENCE      = 55
MAX_OPEN_POSITIONS  = 5
SCAN_INTERVAL_MINS  = 30
DAILY_LOSS_LIMIT    = 150.00
LOG_FILE            = "claudebot_log.json"

# ─────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

# ─────────────────────────────────────────────────────
#  STATE — persisted to claudebot_log.json in the repo
# ─────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            state = json.load(f)
            log(f"📂 Loaded existing state — {len(state.get('trades',[]))} trades, bankroll ${state.get('bankroll', STARTING_BANKROLL):.2f}")
            return state
    log("📂 No existing log found — starting fresh")
    return {
        "bankroll": STARTING_BANKROLL,
        "trades": [],
        "daily_loss": 0.0,
        "daily_reset": datetime.now().strftime("%Y-%m-%d"),
        "scan_count": 0,
        "started": datetime.now().isoformat(),
    }

def save_state(state):
    with open(LOG_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log(f"💾 State saved — bankroll ${state['bankroll']:.2f}, {len(state['trades'])} total trades")

def reset_daily_loss_if_needed(state):
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_loss"] = 0.0
        state["daily_reset"] = today
        log("📅 New day — daily loss counter reset")
    return state

# ─────────────────────────────────────────────────────
#  MARKET RESOLUTION
#  Checks Polymarket to see if open bets have resolved
# ─────────────────────────────────────────────────────

def resolve_open_trades(state):
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if not open_trades:
        return state

    log(f"🔍 Checking resolution of {len(open_trades)} open trade(s)...")

    for trade in open_trades:
        try:
            market_id = trade.get("market_id", "")
            if not market_id or market_id.startswith("m0"):
                continue  # skip demo market IDs

            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue

            market = r.json()
            closed = market.get("closed", False)
            active = market.get("active", True)

            if not closed and active:
                continue  # still running

            # Market has resolved — check winner
            outcome_prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
            yes_price = float(outcome_prices[0])
            no_price  = float(outcome_prices[1])

            position = trade["position"]
            won = (yes_price >= 0.99) if position == "YES" else (no_price >= 0.99)

            trade["status"] = "closed"
            trade["won"] = won
            trade["resolved_at"] = datetime.now().isoformat()

            if won:
                payout = trade.get("potential_return", trade["stake"])
                trade["realized_pnl"] = round(payout - trade["stake"], 2)
                state["bankroll"] = round(state["bankroll"] + payout, 2)
                log(f"  ✅ WON — {trade['market'][:60]}")
                log(f"     Payout: ${payout:.2f} | Profit: +${trade['realized_pnl']:.2f} | Bankroll: ${state['bankroll']:.2f}")
            else:
                trade["realized_pnl"] = -trade["stake"]
                state["daily_loss"] = state.get("daily_loss", 0) + trade["stake"]
                log(f"  ❌ LOST — {trade['market'][:60]}")
                log(f"     Lost: -${trade['stake']:.2f} | Bankroll: ${state['bankroll']:.2f}")

        except Exception as e:
            log(f"  ⚠️  Could not check trade {trade.get('id','?')}: {e}")
            continue

    return state

# ─────────────────────────────────────────────────────
#  MARKET FETCHING
# ─────────────────────────────────────────────────────

DEMO_MARKETS = [
    {"id":"m001","question":"Will Bitcoin exceed $120,000 before June 2026?","yes":44,"volume":8200000,"category":"crypto","closes":"2026-06-30"},
    {"id":"m002","question":"Will Ethereum exceed $4,000 before June 2026?","yes":51,"volume":4100000,"category":"crypto","closes":"2026-06-30"},
    {"id":"m003","question":"Will Solana exceed $300 before end of 2026?","yes":38,"volume":1800000,"category":"crypto","closes":"2026-12-31"},
    {"id":"m005","question":"Will the US Federal Reserve cut rates at least once before July 2026?","yes":67,"volume":5500000,"category":"economics","closes":"2026-07-01"},
    {"id":"m007","question":"Will the US enter a recession before end of 2026?","yes":34,"volume":3800000,"category":"economics","closes":"2026-12-31"},
    {"id":"m010","question":"Will the S&P 500 be higher on December 31 2026 than January 1 2026?","yes":61,"volume":6700000,"category":"economics","closes":"2026-12-31"},
    {"id":"m011","question":"Will there be a ceasefire agreement in Ukraine before July 2026?","yes":29,"volume":4200000,"category":"geopolitics","closes":"2026-07-01"},
    {"id":"m013","question":"Will OpenAI release a new flagship model before July 2026?","yes":81,"volume":2100000,"category":"tech","closes":"2026-07-01"},
    {"id":"m017","question":"Will a European team win the 2026 FIFA World Cup?","yes":52,"volume":2800000,"category":"sports","closes":"2026-07-20"},
]

def fetch_markets():
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50&order=volume&ascending=false"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        raw = r.json()
        markets = []
        for m in raw:
            if not m.get("question") or not m.get("outcomePrices"):
                continue
            prices = json.loads(m["outcomePrices"])
            yes = round(float(prices[0]) * 100)
            markets.append({
                "id": m.get("id", ""),
                "question": m["question"],
                "yes": yes,
                "volume": float(m.get("volume", 0)),
                "category": (m.get("tags") or [{}])[0].get("label", "general"),
                "closes": m.get("endDate", ""),
                "clobTokenIds": m.get("clobTokenIds", []),
            })
        log(f"✅ Fetched {len(markets)} live markets from Polymarket")
        return markets
    except Exception as e:
        log(f"⚠️  Polymarket API unavailable ({e}). Using demo markets.")
        return DEMO_MARKETS

# ─────────────────────────────────────────────────────
#  CLAUDE ANALYSIS
# ─────────────────────────────────────────────────────

def analyze_markets(markets, state):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Markets we already have open positions in — skip these
    open_market_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    open_questions  = {t["market"][:50] for t in state["trades"] if t["status"] == "open"}

    if open_market_ids:
        log(f"📌 Skipping {len(open_market_ids)} markets with existing open positions")

    candidates = [
        m for m in markets
        if m["id"] not in open_market_ids
        and not any(m["question"][:50] in q for q in open_questions)
    ]

    if not candidates:
        log("No new markets to analyze — all slots filled or no new markets.")
        return []

    open_count = sum(1 for t in state["trades"] if t["status"] == "open")
    available_slots = MAX_OPEN_POSITIONS - open_count

    if available_slots <= 0:
        log(f"Max open positions ({MAX_OPEN_POSITIONS}) reached — skipping.")
        return []

    mkt_list = "\n".join([
        f'- ID:{m["id"]} | "{m["question"]}" | YES={m["yes"]}¢ NO={100-m["yes"]}¢ | Vol=${m["volume"]:,.0f} | Closes:{m["closes"]}'
        for m in candidates[:20]
    ])

    open_positions_context = ""
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if open_trades:
        open_positions_context = "\n\nEXISTING OPEN POSITIONS (do not re-recommend these):\n"
        open_positions_context += "\n".join([
            f'- {t["market"][:80]} | {t["position"]} @ {t["entry_price"]}¢'
            for t in open_trades
        ])

    prompt = f"""You are a sharp prediction market trader. Today is {datetime.now().strftime("%B %d, %Y")}.

Scan these Polymarket markets and identify the BEST {min(available_slots, 3)} trades with genuine mispricing.

Bankroll: ${state['bankroll']:.2f} | Max bet: {MAX_BET_PCT}% | Available slots: {available_slots}
{open_positions_context}

Markets:
{mkt_list}

Rules:
- Find mispricings of at least 5-10%
- Do NOT recommend markets from the existing open positions list
- Avoid correlated bets on the same theme
- Be decisive

Return ONLY a JSON array, no other text:
[
  {{
    "market_id": "exact ID from list",
    "market": "exact question text",
    "position": "YES or NO",
    "market_prob": 44,
    "true_prob": 58,
    "confidence": 72,
    "size_pct": 3,
    "reason": "1-2 sentence reason"
  }}
]

If genuinely no edge exists, return: []"""

    log("🤖 Calling Claude to analyze markets...")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        if not raw.startswith("["):
            import re
            match = re.search(r'\[[\s\S]*\]', raw)
            raw = match.group(0) if match else "[]"

        recs = json.loads(raw)
        log(f"🤖 Claude returned {len(recs)} recommendation(s)")
        return recs
    except Exception as e:
        log(f"❌ Claude API error: {e}")
        return []

# ─────────────────────────────────────────────────────
#  KELLY SIZING
# ─────────────────────────────────────────────────────

def kelly_size(true_prob_pct, market_prob_pct, bankroll):
    if not market_prob_pct or market_prob_pct <= 0 or market_prob_pct >= 100:
        return 0
    if not true_prob_pct or true_prob_pct <= 0 or true_prob_pct >= 100:
        return 0

    p = true_prob_pct / 100
    q = 1 - p
    b = (1 - market_prob_pct / 100) / (market_prob_pct / 100)

    if b <= 0:
        return 0

    full_kelly = (b * p - q) / b
    half_kelly  = full_kelly / 2
    capped      = min(max(half_kelly, 0), MAX_BET_PCT / 100)
    return round(capped * bankroll, 2)

# ─────────────────────────────────────────────────────
#  PAPER TRADE EXECUTION
# ─────────────────────────────────────────────────────

def place_paper_trade(rec, markets, state):
    confidence = rec.get("confidence", 0)
    if confidence < MIN_CONFIDENCE:
        log(f"  ⏭  Skipping — confidence {confidence}% below threshold {MIN_CONFIDENCE}%")
        return state

    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    if rec.get("market_id") in open_ids:
        log(f"  ⏭  Skipping — already have open position in this market")
        return state

    open_count = sum(1 for t in state["trades"] if t["status"] == "open")
    if open_count >= MAX_OPEN_POSITIONS:
        log(f"  ⏭  Skipping — max open positions ({MAX_OPEN_POSITIONS}) reached")
        return state

    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit hit — no more trades today")
        return state

    stake = kelly_size(rec["true_prob"], rec["market_prob"], state["bankroll"])
    if stake < 1.00:
        log(f"  ⏭  Skipping — stake too small (${stake:.2f})")
        return state

    entry_price      = rec["market_prob"] if rec["position"] == "YES" else (100 - rec["market_prob"])
    potential_return = round(stake * 100 / entry_price, 2)
    potential_profit = round(potential_return - stake, 2)

    trade = {
        "id":               f"T{int(time.time())}",
        "market_id":        rec["market_id"],
        "market":           rec["market"],
        "position":         rec["position"],
        "entry_price":      entry_price,
        "stake":            stake,
        "potential_return": potential_return,
        "potential_profit": potential_profit,
        "confidence":       confidence,
        "true_prob":        rec["true_prob"],
        "market_prob":      rec["market_prob"],
        "reason":           rec.get("reason", ""),
        "status":           "open",
        "placed_at":        datetime.now().isoformat(),
        "paper":            True,
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  ✅ PAPER BET PLACED")
    log(f"     Market:   {trade['market'][:70]}")
    log(f"     Position: {trade['position']} @ {entry_price}¢")
    log(f"     Stake:    ${stake:.2f} | Win: ${potential_return:.2f} | Profit: +${potential_profit:.2f}")
    log(f"     Confidence: {confidence}% | Reason: {trade['reason']}")
    log(f"     Bankroll remaining: ${state['bankroll']:.2f}")

    return state

# ─────────────────────────────────────────────────────
#  PORTFOLIO SUMMARY
# ─────────────────────────────────────────────────────

def print_portfolio(state):
    trades   = state["trades"]
    open_t   = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    won_t    = [t for t in closed_t if t.get("won")]
    lost_t   = [t for t in closed_t if not t.get("won")]
    realized = sum(t.get("realized_pnl", 0) for t in closed_t)
    win_rate = (len(won_t) / len(closed_t) * 100) if closed_t else 0

    print("\n" + "═" * 55)
    print("  CLAUDEBOT PORTFOLIO")
    print("═" * 55)
    print(f"  Bankroll:        ${state['bankroll']:.2f}")
    print(f"  Realized P&L:    ${realized:+.2f}")
    print(f"  Open Positions:  {len(open_t)}")
    print(f"  Closed Trades:   {len(closed_t)} ({len(won_t)}W / {len(lost_t)}L — {win_rate:.0f}% win rate)")
    print(f"  Total Scans:     {state.get('scan_count', 0)}")
    print("═" * 55)
    if open_t:
        print("\n  OPEN POSITIONS:")
        for t in open_t:
            print(f"  • {t['position']} | ${t['stake']:.2f} | {t['market'][:55]}")
    print()

# ─────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────

def single_scan():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v2 — Single Scan                          ║")
    print(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                              ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌ ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    state = load_state()
    state = reset_daily_loss_if_needed(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    # 1. Resolve any closed markets
    state = resolve_open_trades(state)

    # 2. Fetch live markets
    markets = fetch_markets()

    # 3. Analyze + place trades
    recs = analyze_markets(markets, state)

    if not recs:
        log("No new trades this scan.")
    else:
        for rec in recs:
            log(f"\n→ Evaluating: BUY {rec['position']} on \"{rec['market'][:60]}\"")
            log(f"   Market: {rec['market_prob']}% | Claude: {rec['true_prob']}% | Confidence: {rec['confidence']}%")
            state = place_paper_trade(rec, markets, state)

    # 4. Save — workflow then commits this file back to the repo
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v2 — Continuous Mode                      ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌ ERROR: ANTHROPIC_API_KEY not set.")
        return

    while True:
        try:
            single_scan()
            log(f"💤 Sleeping {SCAN_INTERVAL_MINS} minutes...\n")
            time.sleep(SCAN_INTERVAL_MINS * 60)
        except KeyboardInterrupt:
            log("🛑 Stopped.")
            break
        except Exception as e:
            log(f"❌ Error: {e} — retrying in 60s")
            time.sleep(60)


if __name__ == "__main__":
    if "--single-scan" in sys.argv:
        single_scan()
    else:
        run_loop()
