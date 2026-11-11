import argparse
import dataclasses
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from auth import interactive_qr_login, parse_cookie_string
from bili_client import BiliClient
from ledger import BetLedger
from quant import (
    evaluate_prediction_options,
    parse_hours_to_close,
    parse_pool_coins,
)

# Bilibili's traditional Esports Match Center caps each question at 10 coins (code=75204 if exceeded).
ESPORTS_MAX_COINS_PER_QUESTION = 10

CATEGORY_MAP = {
    "all": (0, "All"),
    "game": (1, "Gaming"),
    "sport": (2, "Sports"),
    "ent": (3, "Entertainment"),
    "tech": (4, "Tech"),
    "acg": (5, "ACG"),
    "life": (6, "Lifestyle"),
    "other": (99, "Other"),
}


def cmd_login(args: argparse.Namespace) -> None:
    client = BiliClient()
    if args.cookie:
        cookies = parse_cookie_string(args.cookie)
        client.save_cookies(cookies)
        nav = client.get_nav_info()
        if nav.get("code") == 0 and nav.get("data", {}).get("isLogin"):
            uname = nav["data"].get("uname")
            coins = client.get_coin_balance()
            print(f"Cookie imported successfully! Account: {uname} | Coin balance: {coins}")
        else:
            print("Warning: Cookie saved, but login verification failed. Check SESSDATA and bili_jct.")
        return

    interactive_qr_login(client)


def sync_plaza_settlements(client: BiliClient, ledger: BetLedger, pages: int = 2) -> int:
    """Syncs settled Prediction Plaza records (`show_status == 3`) into local SQLite ledger."""
    updated = 0
    seen_ids = set()
    for page in range(1, pages + 1):
        res = client.get_plaza_mine_records(page=page, page_size=20)
        if res.get("code") != 0:
            break
        rec_list = (res.get("data") or {}).get("list") or []
        if not rec_list:
            break
        for r in rec_list:
            pid = int(r.get("predict_id") or 0)
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            if int(r.get("show_status") or 0) == 3:
                try:
                    gain = float(r.get("total_profit") or 0.0)
                except (ValueError, TypeError):
                    gain = 0.0
                status = "WON" if gain >= 0.0 else "LOST"
                updated += ledger.update_settlement(pid, status, gain, source="plaza")
    return updated


def cmd_status(args: argparse.Namespace) -> None:
    client = BiliClient()
    ledger = BetLedger()
    print("=" * 75)
    print("BiliBet-AI - Account & Local Ledger Status")
    print("=" * 75)
    if client.is_logged_in:
        nav = client.get_nav_info()
        data = nav.get("data") or {}
        if data.get("isLogin"):
            coins = client.get_coin_balance()
            print(f"Login Status : Logged in ({data.get('uname')}, UID: {data.get('mid')})")
            print(f"Coin Balance : {coins:.1f} coins")
            try:
                ov = client.get_plaza_mine_overview()
                if ov.get("code") == 0 and ov.get("data"):
                    od = ov["data"]
                    print(
                        f"Plaza Summary: Available {od.get('balance')} coins | "
                        f"6M Net Profit {od.get('half_year_profit')} coins | Accuracy {od.get('accuracy')}%"
                    )
            except Exception:
                pass

            if not args.no_sync:
                try:
                    sync_plaza_settlements(client, ledger)
                except Exception:
                    pass
        else:
            print("Login Status : Local cookies expired. Run `python cli.py login` to re-authenticate.")
    else:
        print(
            "Login Status : Not logged in (market scanning & odds calc work without login; run `python cli.py login` before betting)."
        )

    summary = ledger.get_summary()
    if summary["total_bets"] > 0:
        print(
            f"Local Ledger : {summary['total_bets']} total | "
            f"Won {summary['won']} | Lost {summary['lost']} | "
            f"Pending {summary['pending']} ({summary['pending_coins']} coins) | "
            f"Realized PnL {summary['realized_pnl']:+.1f} coins"
        )

    if args.export:
        out = Path(args.export)
        exported = ledger.export_records(out)
        print(f"Exported {exported} ledger record(s) to {out}")

    bets = ledger.list_bets(limit=args.limit)
    print(f"\nRecent Local Bets (latest {len(bets)}):")
    if not bets:
        print("  No local bets recorded yet.")
    else:
        for b in bets:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(b["created_at"]))
            st = b.get("status", "PENDING")
            if st == "WON":
                badge = f"WON {b.get('actual_gain', 0.0):+.1f}"
            elif st == "LOST":
                badge = f"LOST {b.get('actual_gain', 0.0):+.1f}"
            else:
                badge = "PENDING"
            print(
                f"  [{ts}] [{badge}] [{b['source']}#{b['predict_id']}] {b['title']} -> "
                f"[{b['opt_desc']}] {b['coins']} coins (entry odds: {b['effective_odds']:.2f}x, EV: {b['ev_ratio']*100:+.1f}%)"
            )


def cmd_analytics(args: argparse.Namespace) -> None:
    client = BiliClient()
    ledger = BetLedger()
    if client.is_logged_in and not args.no_sync:
        try:
            sync_plaza_settlements(client, ledger)
        except Exception:
            pass

    metrics = ledger.get_analytics()
    pf = metrics["profit_factor"]
    if pf is not None and math.isinf(pf):
        metrics_json = dict(metrics, profit_factor="INF")
    else:
        metrics_json = dict(metrics)

    if args.export:
        out = Path(args.export)
        exported = ledger.export_records(out)
        metrics_json["exported_rows"] = exported
        metrics_json["export_path"] = str(out)

    if args.json:
        print(json.dumps(metrics_json, ensure_ascii=False, indent=2))
        return

    print("=" * 78)
    print("BiliBet-AI - Quantitative Portfolio Analytics")
    print("=" * 78)
    print(
        f"Total Bets     : {metrics['total_bets']} "
        f"(Settled: {metrics['settled_bets']} | Won: {metrics['won']} | Lost: {metrics['lost']} | "
        f"Pending: {metrics['pending']} [{metrics['pending_coins']} coins])"
    )
    print(f"Settled Stake  : {metrics['settled_staked_coins']} coins")
    print(
        f"Realized PnL   : {metrics['realized_pnl']:+.2f} coins "
        f"(Gross Profit: +{metrics['gross_profit']:.2f} | Gross Loss: -{metrics['gross_loss']:.2f})"
    )
    pf_str = "N/A" if pf is None else ("INF (0 losses)" if math.isinf(pf) else f"{pf:.2f}")
    brier_str = (
        f"{metrics['brier_score']:.4f} (n={metrics['calibrated_samples']}, lower is better)"
        if metrics["brier_score"] is not None
        else "N/A (no settled bets with recorded --prob)"
    )
    print(f"Win Rate & ROI : {metrics['win_rate_pct']:.1f}% Win Rate | {metrics['roi_pct']:+.2f}% Settled ROI")
    print(f"Profit Factor  : {pf_str}")
    print(
        f"Drawdown       : Max {metrics['max_drawdown_coins']:.2f} coins | "
        f"Current {metrics['current_drawdown_coins']:.2f} coins"
    )
    print(f"Brier Score    : {brier_str}")
    if metrics["by_source"]:
        print("\nChannel Breakdown:")
        for src, bucket in sorted(metrics["by_source"].items()):
            print(
                f"  - {src:<8}: {bucket['bets']} bets ({bucket['settled']} settled), "
                f"staked {bucket['staked_coins']} coins, PnL {bucket['realized_pnl']:+.2f} coins"
            )
    if args.export:
        print(f"\nExported {metrics_json['exported_rows']} ledger record(s) to {metrics_json['export_path']}")


def cmd_scan(args: argparse.Namespace) -> None:
    client = BiliClient()
    categories_to_scan: List[str] = []
    if args.category == "by-tab":
        categories_to_scan = ["game", "sport", "ent", "tech", "acg", "life", "other"]
    else:
        categories_to_scan = [args.category]

    all_plaza_items: Dict[int, Dict[str, Any]] = {}
    for cat_key in categories_to_scan:
        cid, cname = CATEGORY_MAP[cat_key]
        predicts, _ = client.get_plaza_all_items(
            classify_id=cid,
            state_filter=1,
            order_type=0,
            expand_groups=not args.no_expand,
        )
        for p in predicts:
            pid = p["id"]
            if pid not in all_plaza_items:
                p["_category"] = cname
                all_plaza_items[pid] = p
            elif p.get("_category") == "All" and cname != "All":
                all_plaza_items[pid]["_category"] = cname

    plaza_list = list(all_plaza_items.values())
    for p in plaza_list:
        disp = p.get("display_text", "")
        p["_pool_coins"] = parse_pool_coins(disp)
        p["_hours_to_close"] = parse_hours_to_close(disp)

    if args.max_hours > 0:
        plaza_list = [
            p
            for p in plaza_list
            if p.get("_hours_to_close") is not None and p["_hours_to_close"] <= args.max_hours
        ]

    if args.with_odds:
        for p in plaza_list:
            pid = p["id"]
            for opt in p.get("options") or []:
                pred10 = client.calc_plaza_award(pid, int(opt["opt_idx"]), 10)
                opt["effective_odds_10"] = round(pred10 / 10.0, 3) if pred10 > 0 else 0.0
                opt["predicted_10"] = pred10

    esports_list: List[Dict[str, Any]] = []
    if args.include_esports:
        raw_esports = client.get_esports_questions()
        for item in raw_esports:
            contest = item.get("contest") or {}
            season = contest.get("season") or {}
            for q in item.get("questions") or []:
                esports_list.append(
                    {
                        "contest_id": contest.get("id"),
                        "question_id": q.get("id"),
                        "season": season.get("title", ""),
                        "stage": contest.get("game_stage", ""),
                        "start_time": contest.get("stime", 0),
                        "title": q.get("title", ""),
                        "is_guess": q.get("is_guess", 0),
                        "options": [
                            {
                                "detail_id": d.get("detail_id"),
                                "option": d.get("option"),
                                "odds": d.get("odds"),
                            }
                            for d in (q.get("details") or [])
                        ],
                    }
                )

    if args.json:
        print(
            json.dumps(
                {"plaza": plaza_list, "esports": esports_list},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print("=" * 85)
    print(f"[Prediction Plaza] Found {len(plaza_list)} active market(s)")
    print("=" * 85)
    for p in plaza_list:
        cat = p.get("_category", "All")
        grp = f" [Topic:{p['_group_title']}]" if p.get("_group_title") else ""
        hrs = p.get("_hours_to_close")
        hrs_tag = f" | ~{hrs:.1f}h to close" if hrs is not None else ""
        print(f"\n[#{p['id']}] [{cat}]{grp} {p['title']}")
        print(f"   Pool / Status: {p.get('display_text', '')}{hrs_tag}")
        for opt in p.get("options") or []:
            odds_str = (
                f" | 10-coin snapshot odds: {opt['effective_odds_10']:.2f}x (returns {opt['predicted_10']})"
                if "effective_odds_10" in opt
                else ""
            )
            print(f"   - Option {opt['opt_idx']}: {opt['opt_desc']} (crowd: {opt.get('percent', 0)}%{odds_str})")

    if esports_list:
        print("\n" + "=" * 85)
        print(f"[Esports Match Center (/x/esports/guess)] Found {len(esports_list)} active market(s)")
        print("=" * 85)
        for eq in esports_list:
            start_str = time.strftime("%m-%d %H:%M", time.localtime(eq["start_time"])) if eq["start_time"] else ""
            opts_str = " vs ".join(
                f"{o['option']} (odds:{o['odds']}x, detail_id:{o['detail_id']})" for o in eq["options"]
            )
            print(
                f"[cid={eq['contest_id']} qid={eq['question_id']}] [{eq['season']} {start_str}] {eq['title']} -> {opts_str}"
            )


def cmd_calc(args: argparse.Namespace) -> None:
    client = BiliClient()
    bankroll = args.bankroll
    if bankroll <= 0 and client.is_logged_in:
        bankroll = client.get_coin_balance()
    if bankroll <= 0:
        bankroll = 500.0

    true_probs: Dict[int, float] = {}
    for pair in args.probs.split(","):
        pair = pair.strip()
        if ":" in pair:
            k, v = pair.split(":", 1)
            prob_val = float(v.strip())
            if prob_val < 0.0 or prob_val > 1.0:
                print(f"Error: probability must be within [0.0, 1.0], got '{pair}' (e.g. use 0.65 for 65%).")
                sys.exit(1)
            true_probs[int(k.strip())] = prob_val
    if not true_probs:
        print("Error: no valid option probabilities parsed. Expected format like --probs '1:0.65,2:0.35'.")
        sys.exit(1)

    title = args.title or ""
    pool_coins = args.pool_coins
    hours_to_close: Optional[float] = args.hours_to_close if args.hours_to_close >= 0 else None
    options: List[Dict[str, Any]] = []

    # Automatically enrich metadata (title, options, pool_coins, close_time) from /predict/detail
    try:
        dres = client.get_plaza_detail(args.id)
        if dres.get("code") == 0 and dres.get("data"):
            ddata = dres["data"]
            if not title and ddata.get("title"):
                title = str(ddata["title"])
            if pool_coins <= 0 and ddata.get("total_coin") is not None:
                pool_coins = parse_pool_coins(str(ddata["total_coin"]))
            if hours_to_close is None and ddata.get("close_time"):
                hours_to_close = parse_hours_to_close(str(ddata["close_time"]))
            if ddata.get("options"):
                options = [
                    {
                        "opt_idx": int(o["opt_idx"]),
                        "opt_desc": o.get("opt_desc", f"Option {o['opt_idx']}"),
                        "percent": float(o.get("percent") or 0.0),
                    }
                    for o in ddata["options"]
                    if int(o["opt_idx"]) in true_probs
                ]
    except Exception:
        pass

    if not options:
        options = [{"opt_idx": k, "opt_desc": f"Option {k}", "percent": 0.0} for k in sorted(true_probs.keys())]
    if not title:
        title = f"Prediction Plaza #{args.id}"

    analyses = evaluate_prediction_options(
        predict_id=args.id,
        options=options,
        true_probs=true_probs,
        calc_fn=client.calc_plaza_award,
        bankroll=bankroll,
        kelly_fraction=args.kelly,
        max_single_bet=args.max_bet,
        pool_coins=pool_coins,
        hours_to_close=hours_to_close,
        ignore_drift=args.ignore_drift,
        snipe_window_hours=args.snipe_window,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "predict_id": args.id,
                    "title": title,
                    "bankroll": bankroll,
                    "pool_coins": pool_coins,
                    "hours_to_close": hours_to_close,
                    "analyses": [dataclasses.asdict(a) for a in analyses],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    pool_desc = f"{pool_coins:.0f} coins" if pool_coins > 0 else "Unknown"
    hrs_desc = f"{hours_to_close:.1f}h" if hours_to_close is not None else "Unknown"

    print("=" * 84)
    print(f"[+EV & Parimutuel Drift Analysis] {title} (ID: #{args.id})")
    print(
        f"Bankroll: {bankroll:.1f} | Kelly Fraction: {args.kelly:.2f} | Max Bet: {args.max_bet} | "
        f"Pool: {pool_desc} | Closes In: {hrs_desc} | Snipe Window: <={args.snipe_window:.0f}h"
    )
    print("=" * 84)
    for a in analyses:
        print(
            f"Option {a.opt_idx} [{a.opt_desc}] (Crowd share: {a.crowd_percent:.0f}%):\n"
            f"  - Objective Win Prob (p_true) : {a.true_prob*100:.1f}%\n"
            f"  - 10-Coin Snapshot Odds       : {a.effective_odds_10:.2f}x (Snapshot EV: {a.ev_ratio*100:+.1f}%)\n"
            f"  - Drift-Discounted Odds       : {a.terminal_odds_10:.2f}x (Retention: {a.drift_retention*100:.0f}%, "
            f"Conservative EV: {a.conservative_ev_ratio*100:+.1f}%, Edge: {a.edge_pp:+.1f} pp)\n"
            f"  - Tier                        : [{a.tier}] {a.note}\n"
            f"  - Recommended Kelly Stake     : {a.recommended_coins} coins "
            f"(Est. Return: {a.expected_return_coins:.1f} coins, Eff. Odds: {a.effective_odds_at_rec:.2f}x, "
            f"Expected Net Profit: {a.expected_net_profit:+.2f} coins)"
        )


def cmd_bet(args: argparse.Namespace) -> None:
    if args.coins <= 0:
        print("Error: bet amount (--coins) must be greater than 0.")
        sys.exit(1)
    client = BiliClient()
    ledger = BetLedger()
    if not args.dry_run and not client.is_logged_in:
        print("Error: Not logged into Bilibili! Run `python cli.py login` first (or pass --dry-run to simulate).")
        sys.exit(1)

    if args.source == "esports":
        if not args.qid or not args.detail_id:
            print("Error: esports bet requires --id (contest_id), --qid (question_id), and --detail-id (detail_id).")
            sys.exit(1)
        if args.coins > ESPORTS_MAX_COINS_PER_QUESTION:
            print(
                f"Error: Esports Match Center (/x/esports/guess/add) caps each question at {ESPORTS_MAX_COINS_PER_QUESTION} coins; "
                f"got --coins={args.coins} (exceeding triggers code=75204)."
            )
            sys.exit(1)

    bankroll = args.bankroll
    if bankroll <= 0.0 and client.is_logged_in:
        try:
            bankroll = client.get_coin_balance()
        except Exception:
            bankroll = 0.0

    target_pid = args.id if args.source == "plaza" else args.qid
    risk_check = ledger.check_risk_guardrails(
        proposed_coins=args.coins,
        bankroll=bankroll,
        max_exposure_pct=args.max_exposure_pct,
        max_drawdown_coins=args.max_drawdown,
        source=args.source,
        predict_id=target_pid,
        prevent_duplicate_pending=args.no_duplicate,
    )
    if not risk_check["allowed"]:
        print("Risk Guardrail Blocked Execution:")
        for reason in risk_check["reasons"]:
            print(f"  - {reason}")
        sys.exit(1)

    if args.source == "plaza":
        est = client.calc_plaza_award(args.id, args.opt, args.coins)
        eff_odds = est / float(args.coins) if est > 0 and args.coins > 0 else 0.0
        ev_ratio = (args.prob * eff_odds - 1.0) if args.prob > 0 and eff_odds > 0 else 0.0
        if args.dry_run:
            print(
                f"[DRY-RUN] Simulated [Prediction Plaza #{args.id}] Option {args.opt} | Stake: {args.coins} coins | "
                f"Snapshot Return: {est:.1f} coins ({eff_odds:.2f}x) | EV: {ev_ratio*100:+.1f}% | "
                f"Projected Exposure: {risk_check['projected_exposure_pct']:.1f}% (No API mutation or ledger write)."
            )
            return
        print(
            f"Placing bet on [Prediction Plaza #{args.id}] Option {args.opt} with {args.coins} coins "
            f"(snapshot est. return {est:.1f} coins, {eff_odds:.2f}x)..."
        )
        res = client.place_plaza_bet(predict_id=args.id, opt_idx=args.opt, coins=args.coins)
        if res.get("code") == 0:
            print("Bet placed successfully! Response:", json.dumps(res.get("data", {}), ensure_ascii=False))
            ledger.record_bet(
                source="plaza",
                predict_id=args.id,
                title=args.title or f"Plaza#{args.id}",
                opt_idx=args.opt,
                opt_desc=args.opt_desc or f"Option {args.opt}",
                coins=args.coins,
                effective_odds=eff_odds,
                true_prob=args.prob,
                ev_ratio=ev_ratio,
                expected_return=est,
                reason=args.reason,
            )
        else:
            print(f"Bet failed (code={res.get('code')}): {res.get('message')}")
            sys.exit(1)

    elif args.source == "esports":
        ev_ratio = (args.prob * args.odds - 1.0) if args.prob > 0 and args.odds > 0 else 0.0
        if args.dry_run:
            print(
                f"[DRY-RUN] Simulated [Esports cid={args.id} qid={args.qid}] detail_id={args.detail_id} | "
                f"Stake: {args.coins} coins | Odds: {args.odds:.2f}x | EV: {ev_ratio*100:+.1f}% | "
                f"Projected Exposure: {risk_check['projected_exposure_pct']:.1f}% (No API mutation or ledger write)."
            )
            return
        print(f"Placing bet on [Esports cid={args.id} qid={args.qid}] detail_id={args.detail_id} with {args.coins} coins...")
        res = client.place_esports_bet(
            contest_id=args.id,
            question_id=args.qid,
            detail_id=args.detail_id,
            coins=args.coins,
        )
        if res.get("code") == 0:
            print("Esports bet placed successfully!")
            ledger.record_bet(
                source="esports",
                predict_id=args.qid,
                title=args.title or f"Esports#{args.id}_Q{args.qid}",
                opt_idx=args.detail_id,
                opt_desc=args.opt_desc or str(args.detail_id),
                coins=args.coins,
                effective_odds=args.odds,
                true_prob=args.prob,
                ev_ratio=ev_ratio,
                expected_return=args.coins * args.odds,
                reason=args.reason,
            )
        else:
            print(f"Esports bet failed (code={res.get('code')}): {res.get('message')}")
            sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BiliBet-AI: Parimutuel +EV Scanner & Fractional Kelly CLI for Bilibili Prediction Markets"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Authenticate via QR code or browser Cookie string")
    p_login.add_argument("--cookie", type=str, default="", help="Paste raw browser Cookie header string directly")
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="Show account coin balance, auto-sync settled PnL, and view local ledger")
    p_status.add_argument("--no-sync", action="store_true", help="Skip syncing settled outcomes from /predict/mine/records")
    p_status.add_argument("--limit", type=int, default=15, help="Number of recent local ledger bets to display")
    p_status.add_argument("--export", type=str, default="", help="Optional path to export ledger (.csv or .json)")
    p_status.set_defaults(func=cmd_status)

    p_analytics = sub.add_parser(
        "analytics",
        help="Compute portfolio ROI %, Profit Factor, Max Drawdown, Brier calibration score, and export CSV/JSON",
    )
    p_analytics.add_argument("--no-sync", action="store_true", help="Skip syncing settled outcomes before computing analytics")
    p_analytics.add_argument("--export", type=str, default="", help="Export full ledger history to .csv or .json")
    p_analytics.add_argument("--json", action="store_true", help="Output analytics summary as JSON")
    p_analytics.set_defaults(func=cmd_analytics)

    p_scan = sub.add_parser("scan", help="Scan active Prediction Plaza categories and Esports Match Center markets")
    p_scan.add_argument(
        "--category",
        choices=["all", "by-tab", "game", "sport", "ent", "tech", "acg", "life", "other"],
        default="by-tab",
        help="Category filter (by-tab scans all categories and tags each item)",
    )
    p_scan.add_argument("--no-expand", action="store_true", help="Do not expand nested topic groups")
    p_scan.add_argument("--max-hours", type=float, default=0.0, help="Only show markets closing within <= N hours")
    p_scan.add_argument("--with-odds", action="store_true", help="Probe live 10-coin snapshot odds via /predict/calc")
    p_scan.add_argument("--include-esports", action="store_true", help="Also scan Esports Match Center (/x/esports/guess)")
    p_scan.add_argument("--json", action="store_true", help="Output results as JSON")
    p_scan.set_defaults(func=cmd_scan)

    p_calc = sub.add_parser(
        "calc", help="Run parimutuel odds-drift discounting, snipe-window check, and Fractional Kelly sizing"
    )
    p_calc.add_argument("--id", type=int, required=True, help="Prediction Plaza predict_id")
    p_calc.add_argument("--probs", type=str, required=True, help="Estimated true probabilities, e.g. '1:0.65,2:0.35'")
    p_calc.add_argument("--title", type=str, default="", help="Optional market title override")
    p_calc.add_argument("--bankroll", type=float, default=0.0, help="Coin bankroll for Kelly sizing (defaults to live balance or 500)")
    p_calc.add_argument("--kelly", type=float, default=0.25, help="Fractional Kelly multiplier (default: 0.25 Quarter-Kelly)")
    p_calc.add_argument("--max-bet", type=int, default=100, help="Maximum single-bet coin cap")
    p_calc.add_argument("--pool-coins", type=float, default=0.0, help="Override current pool coins (auto-fetched by default)")
    p_calc.add_argument("--hours-to-close", type=float, default=-1.0, help="Override hours to close (auto-fetched by default)")
    p_calc.add_argument(
        "--snipe-window", type=float, default=24.0, help="Maximum hours to close allowed before flagging SNIPE_LATER (default: 24h)"
    )
    p_calc.add_argument("--ignore-drift", action="store_true", help="Disable odds-drift discounting and snipe-window guard")
    p_calc.add_argument("--json", action="store_true", help="Output Kelly analysis as JSON")
    p_calc.set_defaults(func=cmd_calc)

    p_bet = sub.add_parser("bet", help="Execute (or --dry-run simulate) a coin bet with portfolio risk guardrails")
    p_bet.add_argument("--source", choices=["plaza", "esports"], default="plaza", help="Market channel")
    p_bet.add_argument("--id", type=int, required=True, help="predict_id for plaza, or contest_id for esports")
    p_bet.add_argument("--opt", type=int, default=1, help="Option index opt_idx for plaza (e.g. 1, 2)")
    p_bet.add_argument("--qid", type=int, default=0, help="question_id for esports")
    p_bet.add_argument("--detail-id", type=int, default=0, help="detail_id for esports")
    p_bet.add_argument("--coins", type=int, required=True, help="Coins to wager (esports max: 10 per question)")
    p_bet.add_argument("--title", type=str, default="", help="Market title for ledger record")
    p_bet.add_argument("--opt-desc", type=str, default="", help="Option label for ledger record")
    p_bet.add_argument("--prob", type=float, default=0.0, help="Estimated true win probability")
    p_bet.add_argument("--odds", type=float, default=0.0, help="Decimal odds for esports")
    p_bet.add_argument("--reason", type=str, default="", help="Rationale note for ledger record")
    p_bet.add_argument("--dry-run", action="store_true", help="Simulate odds, EV, and risk checks without placing real bets")
    p_bet.add_argument("--bankroll", type=float, default=0.0, help="Override bankroll for exposure guardrail check")
    p_bet.add_argument(
        "--max-exposure-pct",
        type=float,
        default=0.35,
        help="Max fraction of total equity allowed in active PENDING bets (default: 0.35; set 0 to disable)",
    )
    p_bet.add_argument(
        "--max-drawdown",
        type=float,
        default=0.0,
        help="Drawdown circuit breaker in coins (blocks new bets when current drawdown >= limit; 0 disables)",
    )
    p_bet.add_argument(
        "--no-duplicate",
        action="store_true",
        help="Block execution if a PENDING bet already exists for the same market",
    )
    p_bet.set_defaults(func=cmd_bet)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
