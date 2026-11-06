---
name: bilibili-bet-ai
description: Scan Bilibili Prediction Plaza (Gaming, Sports, Entertainment, Tech, ACG, Lifestyle) and Esports Match Center markets, evaluate real-world probabilities via live web search, probe time-weighted effective odds via /predict/calc, and size optimal coin bets using Parimutuel Fractional Kelly.
---

# BiliBet-AI Agent Skill Workflow

When the user asks you to scan Bilibili prediction markets, recommend positive expected value (**+EV**) coin bets, or execute predictions:

## Step 1: Check Account Status & Scan Active Markets
1. Run `python -X utf8 cli.py status` to check whether the user is logged in and inspect their current coin balance (`bankroll`).
2. If not logged in and the user wants to log in, run `python -X utf8 cli.py login` in the background and share the clickable link to `login_qr.png` (which auto-refreshes every 140s).
3. Run `python -X utf8 cli.py scan --category by-tab --include-esports` to retrieve all active markets across all 8 Prediction Plaza categories and the Esports Match Center.

## Step 2: Filter & Verify Candidate Markets
1. **Capital Turnover Filter**: Prioritize markets closing and settling within the next **1–15 days** when the user's coin balance is small (`< 300` coins), avoiding multi-month lockups unless placing a tiny (`1–5` coin) high-multiplier lottery bet.
2. **Inspect Settlement Rules**: If logged in, query `BiliClient().get_plaza_detail(predict_id)` to verify `settle_reason` (official settlement criteria) and `settle_text` (exact settlement date).
3. **Ground Objective Probability ($p_{\text{true}}$)**: Use web search to verify current standings, brackets (e.g., double-elimination two-life math), macroeconomic data (e.g., CME FedWatch), or external prediction market odds (e.g., Polymarket).

## Step 3: Probe Live Effective Odds & Optimize Kelly Stake
1. Never rely solely on crowd vote percentages (`percent`). Always run:
   ```bash
   python -X utf8 cli.py calc --id <predict_id> --probs "<opt_idx>:<p_true>,..." --bankroll <balance>
   ```
2. Discard any option flagged as `TRAP` ($O_{\text{eff}} \le 1.02$) or `PASS` ($\text{EV} < 5\%$).
3. Present a clear summary table to the user showing: Market ID, Title, Settlement Date, Recommended Option, Live Effective Odds, Estimated $p_{\text{true}}$, $+EV\%$, and Recommended Coin Stake.

## Step 4: Execute Approved Bets
Once the user approves the portfolio, execute each bet sequentially via:
```bash
python -X utf8 cli.py bet --source plaza --id <id> --opt <opt_idx> --coins <n> --title "<title>" --opt-desc "<opt>" --prob <p_true> --reason "<rationale>"
```
(`BiliClient.place_plaza_bet` automatically handles `-702` rate-limit backoff and records every transaction to `data/ledger.db`.)
