---
name: bilibili-bet-ai
description: Scan Bilibili Prediction Plaza (Gaming, Sports, Entertainment, Tech, ACG, Lifestyle) and Esports Match Center markets, evaluate real-world probabilities via live web search, apply parimutuel odds-drift discounting and late-sniping window rules via /predict/calc, and size optimal coin bets using Parimutuel Fractional Kelly.
---

# BiliBet-AI Agent Skill Workflow

When the user asks you to scan Bilibili prediction markets, audit settled profit, recommend positive expected value (**+EV**) coin bets, or execute predictions:

## Step 1: Check Account Status, Auto-Sync Settlements & Scan Markets
1. Run `python -X utf8 cli.py status` to verify login state, inspect live coin balance (`bankroll`), and automatically sync settled Plaza records (`WON` / `LOST` and realized PnL) into `data/ledger.db`.
2. If not logged in and the user wants to log in, run `python -X utf8 cli.py login` in the background and share the clickable link to `login_qr.png` (auto-refreshes every 140s).
3. Run `python -X utf8 cli.py scan --category by-tab --include-esports` (or add `--max-hours 24` to focus strictly on markets inside the 24-hour sniping window).

## Step 2: Filter & Verify Candidate Markets
1. **Parimutuel Late-Sniping Rule (`<= 24h`, ideal `3–8h` before close)**:
   - Bilibili Prediction Plaza uses **floating parimutuel pools (总彩池浮动分红制)**—`/predict/calc` only reflects the *current* snapshot, NOT final settlement odds!
   - For markets closing **`> 24h` away**, retail inflow or smart-money arbitrage can severely dilute early odds. Monitor them and wait until **`3–8h` before close** (when pool maturity exceeds 90%) before committing heavy stakes.
   - For shallow niche pools (`< 30,000` coins), expect other rational bettors to pile into obvious cognitive mispricings before close; always rely on `cli.py calc`'s drift-discounted terminal odds (`terminal_odds_10`) rather than raw snapshot odds.
2. **Inspect Settlement Rules**: Query `BiliClient().get_plaza_detail(predict_id)` to verify `settle_reason` (official settlement criteria) and `settle_text` (exact settlement trigger).
3. **Ground Objective Probability ($p_{\text{true}}$)**: Use web search to verify current standings, brackets, award eligibility rules, macroeconomic data, or external prediction market odds.

## Step 3: Probe Drift-Adjusted Effective Odds & Optimize Kelly Stake
1. Never rely on raw vote percentages (`percent`) or unadjusted early `/predict/calc` snapshots. Run:
   ```bash
   python -X utf8 cli.py calc --id <predict_id> --probs "<opt_idx>:<p_true>,..." --bankroll <balance>
   ```
2. Respect the engine's tier classification:
   - Discard `TRAP` ($O_{\text{eff}} \le 1.00$) and `PASS` (negative conservative EV).
   - Hold off on `SNIPE_LATER` (`hours_to_close > 24h`), advising the user to re-probe within `3–8h` before market close unless they explicitly request an early position (`--ignore-drift`).
3. Present a clear summary table showing: Market ID, Title, Hours to Close, Pool Size, Recommended Option, Static Snapshot Odds vs. Drift-Discounted Terminal Odds, Estimated $p_{\text{true}}$, Conservative $+EV\%$, and Recommended Coin Stake.

## Step 4: Execute Approved Bets
Once the user approves the portfolio, execute each bet sequentially via:
```bash
python -X utf8 cli.py bet --source plaza --id <id> --opt <opt_idx> --coins <n> --title "<title>" --opt-desc "<opt>" --prob <p_true> --reason "<rationale>"
```
(`BiliClient.place_plaza_bet` automatically handles `-702` rate-limit backoff, and `cli.py bet --source esports` enforces Bilibili's 10-coin per-question cap locally.)
