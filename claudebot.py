"""
╔══════════════════════════════════════════════════════╗
║           CLAUDEBOT v3 — Polymarket Paper Trader     ║
║           Short-term only (≤7 days) + Web Research   ║
╚══════════════════════════════════════════════════════╝

Changes in v3:
- Only trades markets closing within MAX_HOLD_DAYS (7 days)
- Claude uses web search to research each market before deciding
- Algorithmic scoring: base rate + recency + volume + momentum
- Much more rational trade reasoning
"""

import time
import json
import os
import sys
import requests
import re
from datetime import datetime, timezone, timedelta
import anthropic

# ─────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
PAPER_TRADING       = True
STARTING_BANKROLL   = 1000.00
MAX_BET_PCT         = 5.0
MIN_CONFIDENCE      = 58
MAX_OPEN_POSITIONS  = 5
MAX_HOLD_DAYS       = 7        # only bet on markets closing within this many days
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
#  STATE
# ─────────────────────────────────────────────────────

def load_state():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            state = json.load(f)
            log(f"📂 Loaded state — {len(state.get('trades',[]))} trades, bankroll ${state.get('bankroll', STARTING_BANKROLL):.2f}")
            return state
    log("📂 No existing log — starting fresh")
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
    log(f"💾 Saved — bankroll ${state['bankroll']:.2f}, {len(state['trades'])} trades")

def reset_daily_loss_if_needed(state):
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("daily_reset") != today:
        state["daily_loss"] = 0.0
        state["daily_reset"] = today
        log("📅 New day — daily loss reset")
    return state

# ─────────────────────────────────────────────────────
#  MARKET RESOLUTION
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
                # Check if demo market has passed its close date
                close_str = trade.get("closes", "")
                if close_str:
                    try:
                        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                        if close_dt.tzinfo is None:
                            close_dt = close_dt.replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) > close_dt:
                            # Demo market expired — simulate 50/50 resolution
                            import random
                            won = random.random() > 0.5
                            trade["status"] = "closed"
                            trade["won"] = won
                            trade["resolved_at"] = datetime.now().isoformat()
                            if won:
                                payout = trade.get("potential_return", trade["stake"])
                                trade["realized_pnl"] = round(payout - trade["stake"], 2)
                                state["bankroll"] = round(state["bankroll"] + payout, 2)
                                log(f"  ✅ [DEMO] WON — {trade['market'][:55]} +${trade['realized_pnl']:.2f}")
                            else:
                                trade["realized_pnl"] = -trade["stake"]
                                state["daily_loss"] = state.get("daily_loss", 0) + trade["stake"]
                                log(f"  ❌ [DEMO] LOST — {trade['market'][:55]} -${trade['stake']:.2f}")
                    except Exception:
                        pass
                continue

            url = f"https://gamma-api.polymarket.com/markets/{market_id}"
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue

            market = r.json()
            closed = market.get("closed", False)
            active = market.get("active", True)

            if not closed and active:
                continue

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
#  MARKET FETCHING + SHORT-TERM FILTER
# ─────────────────────────────────────────────────────

def fetch_markets():
    """
    Fetch markets from Polymarket and filter to only those
    closing within MAX_HOLD_DAYS days. This ensures every
    bet resolves within our holding window.
    """
    try:
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&order=volume&ascending=false"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        raw = r.json()

        now = datetime.now(timezone.utc)
        deadline = now + timedelta(days=MAX_HOLD_DAYS)

        markets = []
        skipped_long = 0

        for m in raw:
            if not m.get("question") or not m.get("outcomePrices"):
                continue

            # Parse close date
            end_date_str = m.get("endDate", "")
            closes_in_days = None
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    closes_in_days = (end_dt - now).total_seconds() / 86400

                    # Skip markets that close too soon (under 2 hours — too late to bet)
                    if closes_in_days < 0.08:
                        continue

                    # Skip markets closing beyond our hold window
                    if closes_in_days > MAX_HOLD_DAYS:
                        skipped_long += 1
                        continue
                except Exception:
                    skipped_long += 1
                    continue
            else:
                skipped_long += 1
                continue

            prices = json.loads(m["outcomePrices"])
            yes = round(float(prices[0]) * 100)

            # Skip markets where odds are already near certainty (>95% or <5%)
            # — not much edge to find there
            if yes >= 95 or yes <= 5:
                continue

            markets.append({
                "id": m.get("id", ""),
                "question": m["question"],
                "yes": yes,
                "volume": float(m.get("volume", 0)),
                "category": (m.get("tags") or [{}])[0].get("label", "general"),
                "closes": end_date_str,
                "closes_in_days": round(closes_in_days, 1),
                "clobTokenIds": m.get("clobTokenIds", []),
            })

        # Sort by closes soonest first — prefer markets resolving sooner
        markets.sort(key=lambda m: m["closes_in_days"])

        log(f"✅ {len(markets)} markets close within {MAX_HOLD_DAYS} days (skipped {skipped_long} longer-term)")
        return markets

    except Exception as e:
        log(f"⚠️  Polymarket API error ({e}). Using demo short-term markets.")
        return get_demo_short_term_markets()


def get_demo_short_term_markets():
    """Demo markets that simulate short-term events."""
    now = datetime.now(timezone.utc)
    return [
        {"id":"d001","question":"Will Bitcoin close above $85,000 today?","yes":52,"volume":1200000,"category":"crypto","closes":(now+timedelta(days=1)).isoformat(),"closes_in_days":1.0},
        {"id":"d002","question":"Will the S&P 500 close up on Friday?","yes":48,"volume":890000,"category":"economics","closes":(now+timedelta(days=2)).isoformat(),"closes_in_days":2.0},
        {"id":"d003","question":"Will Ethereum be above $2,000 by end of week?","yes":61,"volume":740000,"category":"crypto","closes":(now+timedelta(days=4)).isoformat(),"closes_in_days":4.0},
        {"id":"d004","question":"Will the Fed make any emergency announcement this week?","yes":8,"volume":430000,"category":"economics","closes":(now+timedelta(days=5)).isoformat(),"closes_in_days":5.0},
        {"id":"d005","question":"Will BTC dominance exceed 55% by end of week?","yes":44,"volume":320000,"category":"crypto","closes":(now+timedelta(days=6)).isoformat(),"closes_in_days":6.0},
        {"id":"d006","question":"Will there be a major crypto exchange hack this week?","yes":6,"volume":210000,"category":"crypto","closes":(now+timedelta(days=7)).isoformat(),"closes_in_days":7.0},
    ]

# ─────────────────────────────────────────────────────
#  CLAUDE ANALYSIS WITH WEB SEARCH
#  Claude researches each market before deciding
# ─────────────────────────────────────────────────────

def analyze_markets_with_research(markets, state):
    """
    Uses Claude with web_search tool enabled so it can:
    - Look up current prices, news, data
    - Check historical base rates
    - Research the specific event
    - Make a more informed probability estimate
    """
    if not ANTHROPIC_API_KEY:
        log("❌ No API key")
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    open_market_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    open_questions  = {t["market"][:50] for t in state["trades"] if t["status"] == "open"}

    candidates = [
        m for m in markets
        if m["id"] not in open_market_ids
        and not any(m["question"][:50] in q for q in open_questions)
    ]

    if not candidates:
        log("No new markets to analyze.")
        return []

    open_count      = sum(1 for t in state["trades"] if t["status"] == "open")
    available_slots = MAX_OPEN_POSITIONS - open_count

    if available_slots <= 0:
        log(f"Max open positions ({MAX_OPEN_POSITIONS}) reached.")
        return []

    # Build market list for Claude
    mkt_list = "\n".join([
        f'- ID:{m["id"]} | Closes in {m["closes_in_days"]}d | "{m["question"]}" | YES={m["yes"]}¢ NO={100-m["yes"]}¢ | Vol=${m["volume"]:,.0f}'
        for m in candidates[:15]
    ])

    open_positions_ctx = ""
    open_trades = [t for t in state["trades"] if t["status"] == "open"]
    if open_trades:
        open_positions_ctx = "\n\nEXISTING OPEN POSITIONS (skip these):\n"
        open_positions_ctx += "\n".join([f'- {t["market"][:80]} | {t["position"]}' for t in open_trades])

    prompt = f"""You are an algorithmic prediction market trader specialising in SHORT-TERM markets (closing within {MAX_HOLD_DAYS} days).

Today is {datetime.now().strftime("%A, %B %d, %Y %H:%M UTC")}.
Bankroll: ${state['bankroll']:.2f} | Available slots: {available_slots} | Max bet: {MAX_BET_PCT}%
{open_positions_ctx}

SHORT-TERM MARKETS AVAILABLE (all close within {MAX_HOLD_DAYS} days):
{mkt_list}

YOUR RESEARCH PROCESS — for each promising market:
1. USE WEB SEARCH to look up current data relevant to the question
   - For crypto: search current price, 24h trend, recent news
   - For economics: search latest data releases, Fed statements, analyst forecasts  
   - For sports: search current standings, recent form, injury news
   - For politics: search latest polls, news, expert forecasts
2. Apply BASE RATE thinking — how often does this type of event happen?
3. Look for MOMENTUM signals — is the situation moving toward YES or NO?
4. Check if the CROWD IS WRONG — fear/greed, recency bias, overreaction to news
5. Only recommend if you find genuine edge of 7%+ after research

ALGORITHMIC SCORING CRITERIA:
- Edge score: (your true_prob - market_prob) — must be ≥7% to trade
- Confidence: how certain are you of your research findings (50-95%)
- Time decay: markets closing in 1-2 days need higher confidence than 5-7 day markets
- Volume filter: prefer markets with >$100k volume (more liquid, fairer pricing)
- Avoid: markets where you cannot find any relevant data to research

After researching, return ONLY a JSON array (no other text):
[
  {{
    "market_id": "exact ID",
    "market": "exact question",
    "position": "YES or NO",
    "market_prob": 48,
    "true_prob": 62,
    "confidence": 71,
    "size_pct": 3,
    "closes_in_days": 2.0,
    "research_summary": "2-3 sentences summarising what you found and why it gives you edge",
    "key_factors": ["factor 1", "factor 2", "factor 3"],
    "bear_case": "main reason you could be wrong"
  }}
]

If after research you find no genuine edge in any market, return: []"""

    log("🔬 Calling Claude with web search to research markets...")

    try:
        # Enable web_search tool so Claude can research
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search"
            }],
            messages=[{"role": "user", "content": prompt}]
        )

        # Extract all text blocks from response (Claude may search multiple times)
        searches_done = 0
        full_text = ""

        for block in response.content:
            if block.type == "tool_use" and block.name == "web_search":
                searches_done += 1
                query = block.input.get("query", "")
                log(f"  🔍 Claude searched: \"{query}\"")
            elif block.type == "text":
                full_text += block.text

        log(f"  📊 Claude ran {searches_done} web search(es)")

        # Parse JSON from response
        raw = full_text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        if not raw.startswith("["):
            match = re.search(r'\[[\s\S]*\]', raw)
            raw = match.group(0) if match else "[]"

        recs = json.loads(raw)

        # Filter: only keep recs with sufficient edge
        filtered = []
        for r in recs:
            edge = abs(r.get("true_prob", 0) - r.get("market_prob", 0))
            if edge < 7:
                log(f"  ⏭  Filtered out (edge only {edge}%): {r.get('market','')[:50]}")
                continue
            filtered.append(r)

        log(f"🤖 Claude recommends {len(filtered)} trade(s) after research (filtered from {len(recs)})")

        # Log research summaries
        for r in filtered:
            log(f"\n  📋 RESEARCH: {r.get('market','')[:60]}")
            log(f"     Summary: {r.get('research_summary','')}")
            log(f"     Edge: market={r.get('market_prob')}% → Claude={r.get('true_prob')}% (+{abs(r.get('true_prob',0)-r.get('market_prob',0))}%)")
            log(f"     Bear case: {r.get('bear_case','')}")

        return filtered

    except Exception as e:
        log(f"❌ Claude API error: {e}")
        return []

# ─────────────────────────────────────────────────────
#  KELLY SIZING
# ─────────────────────────────────────────────────────

def kelly_size(true_prob_pct, market_prob_pct, bankroll, closes_in_days=7):
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
    half_kelly = full_kelly / 2

    # Slight reduction for very short-term markets (higher variance)
    if closes_in_days <= 1:
        half_kelly *= 0.7  # reduce size for same-day bets
    elif closes_in_days <= 2:
        half_kelly *= 0.85

    capped = min(max(half_kelly, 0), MAX_BET_PCT / 100)
    return round(capped * bankroll, 2)

# ─────────────────────────────────────────────────────
#  PAPER TRADE EXECUTION
# ─────────────────────────────────────────────────────

def place_paper_trade(rec, markets, state):
    confidence = rec.get("confidence", 0)
    if confidence < MIN_CONFIDENCE:
        log(f"  ⏭  Skipping — confidence {confidence}% below {MIN_CONFIDENCE}%")
        return state

    open_ids = {t["market_id"] for t in state["trades"] if t["status"] == "open"}
    if rec.get("market_id") in open_ids:
        log(f"  ⏭  Skipping — already open in this market")
        return state

    open_count = sum(1 for t in state["trades"] if t["status"] == "open")
    if open_count >= MAX_OPEN_POSITIONS:
        log(f"  ⏭  Max positions reached")
        return state

    if state.get("daily_loss", 0) >= DAILY_LOSS_LIMIT:
        log(f"  🛑 Daily loss limit hit")
        return state

    closes_in_days = rec.get("closes_in_days", 7)
    stake = kelly_size(rec["true_prob"], rec["market_prob"], state["bankroll"], closes_in_days)
    if stake < 1.00:
        log(f"  ⏭  Stake too small (${stake:.2f})")
        return state

    entry_price      = rec["market_prob"] if rec["position"] == "YES" else (100 - rec["market_prob"])
    potential_return = round(stake * 100 / entry_price, 2)
    potential_profit = round(potential_return - stake, 2)

    # Find the market to get close date
    market_data = next((m for m in markets if m["id"] == rec["market_id"]), {})

    trade = {
        "id":                f"T{int(time.time())}",
        "market_id":         rec["market_id"],
        "market":            rec["market"],
        "position":          rec["position"],
        "entry_price":       entry_price,
        "stake":             stake,
        "potential_return":  potential_return,
        "potential_profit":  potential_profit,
        "confidence":        confidence,
        "true_prob":         rec["true_prob"],
        "market_prob":       rec["market_prob"],
        "closes_in_days":    closes_in_days,
        "closes":            market_data.get("closes", ""),
        "research_summary":  rec.get("research_summary", ""),
        "key_factors":       rec.get("key_factors", []),
        "bear_case":         rec.get("bear_case", ""),
        "status":            "open",
        "placed_at":         datetime.now().isoformat(),
        "paper":             True,
    }

    state["bankroll"] = round(state["bankroll"] - stake, 2)
    state["trades"].append(trade)

    log(f"  ✅ PAPER BET PLACED")
    log(f"     Market:     {trade['market'][:70]}")
    log(f"     Position:   {trade['position']} @ {entry_price}¢")
    log(f"     Closes in:  {closes_in_days} days")
    log(f"     Stake:      ${stake:.2f} | Win: ${potential_return:.2f} | Edge: +${potential_profit:.2f}")
    log(f"     Confidence: {confidence}%")
    log(f"     Research:   {trade['research_summary'][:100]}")
    log(f"     Bankroll:   ${state['bankroll']:.2f}")

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
    roi      = ((state["bankroll"] - STARTING_BANKROLL) / STARTING_BANKROLL * 100)

    print("\n" + "═" * 60)
    print("  CLAUDEBOT v3 PORTFOLIO")
    print("═" * 60)
    print(f"  Bankroll:        ${state['bankroll']:.2f}  ({roi:+.1f}% ROI)")
    print(f"  Realized P&L:    ${realized:+.2f}")
    print(f"  Open Positions:  {len(open_t)}")
    print(f"  Closed Trades:   {len(closed_t)}  ({len(won_t)}W / {len(lost_t)}L  —  {win_rate:.0f}% win rate)")
    print(f"  Total Scans:     {state.get('scan_count', 0)}")
    print(f"  Max Hold:        {MAX_HOLD_DAYS} days")
    print("═" * 60)

    if open_t:
        print("\n  OPEN POSITIONS:")
        for t in open_t:
            days_left = t.get("closes_in_days", "?")
            print(f"  • {t['position']} | ${t['stake']:.2f} | closes in {days_left}d | {t['market'][:50]}")
    print()

# ─────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────

def single_scan():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v3 — Short-Term + Research Mode           ║")
    print(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Max hold: {MAX_HOLD_DAYS} days        ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    if not ANTHROPIC_API_KEY:
        print("❌ ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    state = load_state()
    state = reset_daily_loss_if_needed(state)
    state["scan_count"] = state.get("scan_count", 0) + 1

    # 1. Resolve any closed markets
    state = resolve_open_trades(state)

    # 2. Fetch only short-term markets
    markets = fetch_markets()

    if not markets:
        log(f"No markets closing within {MAX_HOLD_DAYS} days found — skipping.")
        save_state(state)
        print_portfolio(state)
        return

    log(f"📅 Found {len(markets)} markets closing within {MAX_HOLD_DAYS} days")

    # 3. Claude researches + recommends trades
    recs = analyze_markets_with_research(markets, state)

    if not recs:
        log("No trades recommended after research.")
    else:
        for rec in recs:
            log(f"\n→ BUY {rec['position']} on \"{rec['market'][:60]}\"")
            state = place_paper_trade(rec, markets, state)

    # 4. Save — workflow commits this back to repo
    save_state(state)
    print_portfolio(state)


def run_loop():
    print("\n╔══════════════════════════════════════════════════════╗")
    print("║  CLAUDEBOT v3 — Continuous Mode                      ║")
    print(f"║  Max hold: {MAX_HOLD_DAYS} days | Interval: {SCAN_INTERVAL_MINS}min             ║")
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
