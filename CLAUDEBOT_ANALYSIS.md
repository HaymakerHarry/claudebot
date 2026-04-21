# ClaudeBot + NoBot — Full System Analysis
**Date:** April 21, 2026  
**Scope:** claudebot.py (v13), nobot_v2.py, cross-comparison with workspace research

---

## 📊 PERFORMANCE SNAPSHOT

| Bot | ROI | Win Rate | Net P&L | Trades |
|-----|-----|----------|---------|--------|
| **ClaudeBot v13** | +21.3% | 66.7% (14W/7L) | +$1,214.95 realized | 21 closed, 6 open |
| **NoBot v2** | -21.8% (cash) | 63.8% (113W/64L) | +$122.95 realized | 177 closed, 86 open |

> ⚠️ **NoBot bankroll is misleading.** The -21.8% "ROI" is cash remaining after deploying ~$340 into 86 open positions. Realized P&L is actually +$122.95 (+12.3%) on closed trades. The display just doesn't show deployed capital as portfolio value.

---

## ✅ WHAT'S WORKING WELL — CLAUDEBOT v13

### Architecture
**The Haiku → Opus pipeline is smart.** Using a cheap model ($0.25/MTok) as a screener to pass only high-potential markets to Opus (~$15/MTok) is genuinely cost-efficient. At 133 scans this alone probably saved $100+ in API costs vs. running Opus on everything.

**Adaptive thinking on Opus is correct.** Using `thinking: {"type": "adaptive"}` lets the model reason through uncertainty on hard calls. The log shows Opus catching things like cross-platform discrepancy on the Ye album trade that a shallow model would miss.

**The three-tier cadence is sound.** T1 every scan (3h), T2 once daily, T3 once weekly — this matches the information decay rate for each horizon. You don't need to re-evaluate a 30-day thesis every 3 hours.

**Two-strike reassessment brain is genuinely sophisticated.** The measles market (T1775271291) is a perfect example: Opus entered at 6¢ YES with good linear extrapolation reasoning, but the reassessment correctly identified deceleration (only 8.2 cases/day vs. 18.5 projected), flagged it on WATCH, and will close it next cycle if the pace doesn't recover. That's better risk management than most discretionary traders do.

**Bear case documentation is excellent.** Every trade stores `bear_case`. The ECB trade loss could have been avoided if the "other banks may disagree" bear case had been weighted heavier — but at least it's there for learning.

**DDG search + Haiku brief pipeline is the right architecture.** Real-world data flowing through a fast interpreter before hitting Opus makes the analysis grounded rather than relying on Opus's (possibly stale) training data alone.

**Category diversity cap works.** The `screener_max_per_cat: 2` combined with `MAX_PER_CATEGORY = 1` prevents weather market obsession, which is important because there are hundreds of city temperature markets on Polymarket at any given time.

### Edge Quality
**Weather "statistical mismatch" trades are genuinely profitable.** 6W/2L on weather (75%) with +$256 P&L. The core thesis — "exact temperature matches are statistically rare, market prices them too high" — is a real, exploitable inefficiency. The bot correctly identifies that a 7°C anomaly required in Busan mid-April is a 2-sigma event, and prices that correctly.

**Crypto price level bets with current data are strong.** 3W/1L (75%), +$375. The pattern: when DDG gives a current price that already makes the outcome effectively settled (SOL at $84 with "above $80 today" closing in hours), the edge is near-certain. The BTC loss was a stale data problem, not a thesis problem.

**The news-triggered pathway adds real value.** The Zelenskyy tweet count trade (T1775578495, +$36) was flagged as news-triggered and correctly identified an oddly-mispriced market. Low ROI on this one but the mechanism is sound.

---

## ❌ WHAT ISN'T WORKING — CLAUDEBOT v13

### Critical Problems

**1. Paper trading only — the results are partly fictional.**
`PAPER_TRADING = True` throughout. More importantly, for markets with IDs starting with `"d0"`, the resolution logic falls back to `random.random() > 0.5` — a literal coin flip. Any trade that doesn't have a real market ID resolution gets randomly settled, which completely contaminates the performance data. This is a hidden data quality problem.

**2. The Ye album trade is an outlier that inflates everything.**
Without that single +$634.26 trade, net P&L is +$580. That's still good, but the Ye trade came from an unusually obvious cross-platform pricing discrepancy (88% YES here vs. 25% on Kalshi for the same question). Those opportunities are rare. The bot can't rely on them. Strip it out and evaluate the rest.

**3. Kelly fractions are too aggressive for a $1000 bankroll.**
- WTI NO trade: **$310.98** on a ~$1000 bankroll (31%!) 
- Buenos Aires temp NO: **$198-$205** (20%)
- Busan temp NO: **$205** (20%)
- The "full-Kelly (15%)" tier is genuinely full Kelly, which is mathematically correct for edge, but brutal during drawdown streaks. For a paper test, fine. For real money at $1000, a 3-4 loss streak could wipe 50-60%.

**4. Opus violates its own prompt rules.**
The ECB trade (T1775446385, -$63) was placed despite the explicit instruction: *"❌ Central bank/Fed/ECB decisions based only on analyst forecasts — these require CONFIRMED data releases or explicit official forward guidance."* Barclays and JPMorgan forecasts are literally "analyst forecasts." The prompt failed to stop this. The guard needs to be stronger — more explicit, possibly with a checklist format that Opus must tick off.

**5. Trump speech/tweet bets have a resolution criteria problem.**
The "Bully of the Middle East" trade (-$123): Opus correctly found the phrase had been posted, but it lost. Why? Likely resolution criteria edge cases (exact phrasing, date window, source requirements). The "Trump say 'Christian'" trade (-$185) also lost. These markets have **ambiguous resolution criteria** that even good research can't fully resolve. They should be a flagged category: *"resolution criteria involve exact text match or subjective judgment → proceed only with very high confidence or skip."*

**6. Stale data problem in crypto/stock bets.**
The BTC loss (T1776020593, -$137): "BTC at $71,838" from research was stale. Actual BTC was well above $70K. DDG doesn't give real-time prices. For "is X above $Y right now" type markets, the bot needs a live price feed, not a web search snippet.

**7. No deployment/execution infrastructure.**
There is no Polymarket CLOB API integration. When this goes live, you'll need the `py-clob-client` library and a funded wallet. The paper results also don't account for Polymarket's 0.1–0.2% taker fee, which compounds at scale.

### Minor Issues

**8. Weather "exact temperature" market selection needs refinement.**
Two Lucknow temperature bets were taken — one won, one lost. The issue: "exactly 36°C" vs. "exactly 28°C or below" have different statistical profiles. The "or below" version is much harder to lose because it's a direction bet with a threshold. The bot should prefer direction/threshold markets over exact-value markets, especially at short time horizons where forecast uncertainty is still high.

**9. T2 and T3 have almost no real data yet.**
Only 1 T2 closed trade (loss). Zero T3 closed. The tier system can't be evaluated fairly yet. The ECB trade loss may have been the prompt failing, not the tier system failing.

**10. GitHub Actions always runs nobot.py (v1) which isn't in the analysis.**
The workflow runs `nobot.py`, `nobot_v2.py`, and `claudebot.py`. But `nobot.py` logs are separate. You're paying API costs for 3 bots but only tracking 2 clearly.

---

## ✅ WHAT'S WORKING WELL — NOBOT v2

**Volume-weighted stake sizing is clever.** The sweet spot ($10k–$100k volume) getting a 2.5x multiplier while >$100k gets reduced to 1.0x correctly identifies that high-volume markets have more informed traders (worse edge for you).

**Using Polymarket native tags for sports filtering is better than keyword matching.** The keyword-based category filter in ClaudeBot has gaps — keywords like "beat the," "vs.", and team names constantly need updating. NoBot v2's `market_tags` check from the API is cleaner.

**Single-call Haiku batch screening is cost-efficient.** Rather than screening each market individually, it batches all 100+ markets into one Haiku call to identify news-triggered skips. Smart.

---

## ❌ WHAT ISN'T WORKING — NOBOT v2

**The strategy thesis isn't holding.**
63.8% win rate on NO bets in the 45–62¢ range. The bot needs at minimum 53%+ win rate to break even in this range (average ~53¢ NO price → needs ~53%+ win rate to profit). At 63.8% it should be profitable... and it is on realized P&L (+$122.95 from 177 closed trades). But:

- **Position sizing is too small** to compound meaningfully (~$3–6 per trade on $1000)
- **86 open positions** creates a false "bankroll deficit" that makes the system look broken
- **The bankroll display is misleading** — it shows cash remaining, not portfolio value

If 86 open positions resolve at the historical 63.8% rate, you're looking at ~+$70 additional profit. Total expected outcome: ~$1,193 on $1,000 started — a reasonable +19.3%. The bot isn't broken; the accounting is confusing.

**However:** the "73.3% of Polymarket markets resolve NO" stat may not hold for 45–62¢ NO markets specifically. Those markets are at or near 50/50 by definition — if they were clearly going to resolve NO, the NO price would already be 80–90¢. The edge thesis is shaky. Needs proper statistical validation.

---

## 🔗 WHAT TO BORROW FROM THE OPENCLAW WORKSPACE

Your workspace has significantly more sophisticated infrastructure than ClaudeBot. Here's what maps directly:

### 1. Strategy Evaluation Framework → Apply to Polymarket Strategies
The `STRATEGY_EVALUATION_FRAMEWORK.md` protocol (backtest → robustness check → out-of-sample validation → grade) should be applied to each ClaudeBot strategy type before trusting it with real money:

- **Weather exact-temp markets:** Need historical Polymarket resolution data to validate the "exact integer is underpriced" thesis
- **Crypto price level bets:** Need to validate the "closing day price already past threshold" edge properly
- **NoBot v2 thesis:** Run a proper backtest on historical Polymarket resolution data — does NO in 45–62¢ range really beat 53%?

### 2. Multi-Agent Architecture → Replace Single-File Bot
The workspace has 7+ specialized agents (news, world model, liquidity, execution, etc.). ClaudeBot is one massive 1,700-line file. Splitting into agents would:
- Make each component testable independently
- Allow parallel research calls (currently sequential)
- Let you run a "market regime" check before every scan (are conditions suitable for T1 vs. T3 focus?)

### 3. News Intelligence Agent → Upgrade ClaudeBot's RSS Monitor
The workspace's `news_agent.py` with ForexFactory calendar parsing is more sophisticated than ClaudeBot's RSS feeds. Key missing piece: **economic calendar awareness**. Right now ClaudeBot could enter a trade on "Will S&P 500 be above X?" the day before a FOMC decision — the workspace's news agent would flag that as a high-volatility event to avoid.

### 4. Live Data Sources → Fix the Stale Data Problem
The workspace has integrations for real data. For ClaudeBot specifically:
- **Crypto/stock price bets:** Use CoinGecko API (free) or similar for real-time prices, not DDG search
- **Weather bets:** Use OpenWeatherMap API for actual forecast data, not DDG text snippets
- **Economic data:** Use FRED API for confirmed release data (CPI, jobs, etc.)

### 5. Trade Journal / Learning Agent → Track What's Actually Working
The workspace has a `06-trade-journal-agent`. ClaudeBot stores bear cases but doesn't learn from them. Adding a learning loop: after each closed trade, categorize the loss type (stale data / resolution ambiguity / thesis wrong / market moved / etc.) and feed patterns back to the screener prompt.

---

## 🛠️ CONCRETE IMPROVEMENT PRIORITIES

### High Priority (fix before real money)

**A. Add live price verification for crypto/stock bets**
```python
def get_live_price(symbol):
    # CoinGecko for crypto
    r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd")
    return r.json()[symbol]['usd']
```
Before placing any crypto/stock bet where the current price determines the answer, verify live.

**B. Add resolution criteria scoring**
Before Opus recommends a trade, pass the market question through a check: does it involve exact text matching, subjective judgment, or ambiguous "this week" windows? Trades scoring high on ambiguity get flagged for manual review or skip entirely.

**C. Reduce max position size to 8% cap**
The current `max_pct: 15.0` for full-Kelly T1 is too high. Cap at 8% regardless of confidence. At $1000 bankroll, no single trade should exceed $80.

**D. Fix the bankroll display for NoBot**
Add a "portfolio value" field: `cash + sum of open stakes` = true current value. The current -21.8% figure is misleading and will make it hard to evaluate the strategy fairly.

### Medium Priority (next iteration)

**E. Add economic calendar blocking**
Before any economics/politics market bet, check if there's a major scheduled event (FOMC, CPI, NFP) in the market's resolution window that could flip the outcome on one number. Borrow from the workspace's ForexFactory parser.

**F. Split ClaudeBot into modules**
Break out: `screener.py`, `researcher.py`, `analyst.py`, `executor.py`, `state.py`, `notifier.py`. Makes it testable and maintainable. The current single-file approach is already 1,700 lines and growing.

**G. Add Polymarket CLOB API integration (read-only first)**
Use `py-clob-client` to pull live order book depth alongside the gamma API price. A market priced at 20¢ YES with huge bid depth below 20 is very different from one with a thin book — the former suggests informed money, the latter is just drifting.

**H. Better weather market selection**
Prefer "above X" or "below X" over "exactly X degrees." Exact temperature matches are a statistical quirk — they work but have higher variance. Direction + threshold bets are more replicable.

### Lower Priority (longer term)

**I. Backtest the "exact temperature is underpriced" thesis**
Pull 6 months of Polymarket weather market resolution data and verify that markets priced at 25–40% YES on exact temperature actually resolved YES less than 25% of the time. This is the single most important validation the weather strategy needs.

**J. Multi-currency Polymarket markets**
Most current positions are USD-denominated events. There's edge in less-followed markets (non-US politics, regional economics) where DDG research may be cleaner and the market less efficient.

---

## SUMMARY VERDICT

**ClaudeBot v13** is genuinely impressive for a paper trading prototype. The architecture is well-designed, the multi-tier structure is logical, and the Haiku→Opus pipeline is cost-smart. The real concerns are: (1) the paper results include randomized resolution for some trades, (2) Kelly fractions are dangerously large for a real-money $1000 account, (3) a single Ye album trade generated more P&L than all other trades combined, and (4) there's no live data integration. Fix those four things and this is a viable real-money system.

**NoBot v2** is not broken but its accounting is confusing. Realized P&L is positive. The strategy thesis (buy NO on near-50/50 markets) needs formal statistical validation, but the infrastructure is sound. The Polymarket native tag filtering is a direct improvement worth backporting to ClaudeBot.

The **openclaw workspace** is a treasure chest. The strategy evaluation framework, multi-agent architecture, news intelligence agent, and ForexFactory integration are all directly applicable. The Polymarket bot should borrow the evaluation discipline — "backtest it before you trust it" — above everything else.

---

*Generated April 21, 2026*
