import argparse
import json
import sys
import time
from typing import Any, Dict, List

from auth import interactive_qr_login, parse_cookie_string
from bili_client import BiliClient
from ledger import BetLedger
from quant import (
    evaluate_prediction_options,
    fractional_kelly_fixed_odds,
    parse_pool_coins,
)

CATEGORY_MAP = {
    "all": (0, "全部"),
    "game": (1, "游戏"),
    "sport": (2, "体育"),
    "ent": (3, "文娱"),
    "tech": (4, "科技"),
    "acg": (5, "二次元"),
    "life": (6, "生活"),
    "other": (99, "其他"),
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
            print(f"Cookie 导入成功！当前用户: {uname} | 硬币余额: {coins}")
        else:
            print("警告: Cookie 已保存，但登录态校验未通过，请检查 SESSDATA 和 bili_jct 是否完整。")
        return

    interactive_qr_login(client)


def cmd_status(args: argparse.Namespace) -> None:
    client = BiliClient()
    ledger = BetLedger()
    print("=" * 70)
    print("B站预测投顾系统 - 账户与账本状态")
    print("=" * 70)
    if client.is_logged_in:
        nav = client.get_nav_info()
        data = nav.get("data") or {}
        if data.get("isLogin"):
            coins = client.get_coin_balance()
            print(f"登录状态: 已登录 ({data.get('uname')}, UID: {data.get('mid')})")
            print(f"硬币余额: {coins:.1f} 枚")
            try:
                ov = client.get_plaza_mine_overview()
                if ov.get("code") == 0 and ov.get("data"):
                    od = ov["data"]
                    print(f"预测广场概览: {json.dumps(od, ensure_ascii=False)}")
            except Exception:
                pass
        else:
            print("登录状态: 本地 Cookie 已失效，请重新运行 `python cli.py login`")
    else:
        print("登录状态: 未登录（可免登录扫描盘口与试算赔率；下注前需运行 `python cli.py login`）")

    bets = ledger.list_bets(limit=15)
    print(f"\n本地历史下注记录 (最近 {len(bets)} 笔):")
    if not bets:
        print("  暂无本地下注记录。")
    else:
        for b in bets:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(b["created_at"]))
            print(
                f"  [{ts}] [{b['source']}#{b['predict_id']}] {b['title']} -> "
                f"选[{b['opt_desc']}] 投 {b['coins']} 币 (赔率:{b['effective_odds']:.2f}x, EV:{b['ev_ratio']*100:+.1f}%)"
            )


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
            elif p.get("_category") == "全部" and cname != "全部":
                all_plaza_items[pid]["_category"] = cname

    plaza_list = list(all_plaza_items.values())
    for p in plaza_list:
        p["_pool_coins"] = parse_pool_coins(p.get("display_text", ""))

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
    print(f"【预测广场】共扫描到 {len(plaza_list)} 个进行中盘口")
    print("=" * 85)
    for p in plaza_list:
        cat = p.get("_category", "全部")
        grp = f" [专题:{p['_group_title']}]" if p.get("_group_title") else ""
        print(f"\n[#{p['id']}] [{cat}]{grp} {p['title']}")
        print(f"   状态/奖池: {p.get('display_text', '')}")
        for opt in p.get("options") or []:
            odds_str = (
                f" | 实测10币赔率: {opt['effective_odds_10']:.2f}x (返{opt['predicted_10']}币)"
                if "effective_odds_10" in opt
                else ""
            )
            print(f"   - 选项 {opt['opt_idx']}: {opt['opt_desc']} (大众占比: {opt.get('percent', 0)}%{odds_str})")

    if esports_list:
        print("\n" + "=" * 85)
        print(f"【传统赛事中心 (/x/esports/guess)】共扫描到 {len(esports_list)} 个进行中盘口")
        print("=" * 85)
        for eq in esports_list:
            start_str = time.strftime("%m-%d %H:%M", time.localtime(eq["start_time"])) if eq["start_time"] else ""
            opts_str = " vs ".join(
                f"{o['option']} (赔率:{o['odds']}x, detail_id:{o['detail_id']})" for o in eq["options"]
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

    # Parse --probs like "1:0.65,2:0.35"
    true_probs: Dict[int, float] = {}
    for pair in args.probs.split(","):
        pair = pair.strip()
        if ":" in pair:
            k, v = pair.split(":", 1)
            true_probs[int(k.strip())] = float(v.strip())

    # Fetch option metadata from plaza list or construct from probs
    options = [{"opt_idx": k, "opt_desc": f"选项{k}", "percent": 0.0} for k in sorted(true_probs.keys())]
    if args.title:
        title = args.title
    else:
        title = f"预测广场 #{args.id}"

    analyses = evaluate_prediction_options(
        predict_id=args.id,
        options=options,
        true_probs=true_probs,
        calc_fn=client.calc_plaza_award,
        bankroll=bankroll,
        kelly_fraction=args.kelly,
        max_single_bet=args.max_bet,
    )

    print("=" * 80)
    print(f"【+EV & 分数凯利仓位精算】 {title} (ID: #{args.id})")
    print(f"核算本金池: {bankroll:.1f} 硬币 | 凯利系数: {args.kelly:.2f} | 单笔上限: {args.max_bet} 硬币")
    print("=" * 80)
    for a in analyses:
        print(
            f"选项 {a.opt_idx} [{a.opt_desc}]:\n"
            f"  - AI客观胜率 p_true : {a.true_prob*100:.1f}%\n"
            f"  - 10币实测有效赔率  : {a.effective_odds_10:.2f}x (隐含胜率: {(100.0/a.effective_odds_10 if a.effective_odds_10>0 else 0):.1f}%)\n"
            f"  - 期望值 (EV)       : {a.ev_ratio*100:+.1f}% (胜率优势 Edge: {a.edge_pp:+.1f} pp)\n"
            f"  - 评级              : 【{a.tier}】 {a.note}\n"
            f"  - 凯利建议下注      : {a.recommended_coins} 枚硬币 "
            f"(预计返还: {a.expected_return_coins:.1f} 币, 有效赔率: {a.effective_odds_at_rec:.2f}x, 期望净赚: {a.expected_net_profit:+.2f} 币)"
        )


def cmd_bet(args: argparse.Namespace) -> None:
    client = BiliClient()
    ledger = BetLedger()
    if not client.is_logged_in:
        print("错误: 尚未登录 B站账号！请先运行 `python cli.py login` 扫码或导入 Cookie。")
        sys.exit(1)

    if args.source == "plaza":
        est = client.calc_plaza_award(args.id, args.opt, args.coins)
        eff_odds = est / float(args.coins) if est > 0 and args.coins > 0 else 0.0
        print(f"正在向【预测广场 #{args.id}】选项 {args.opt} 投入 {args.coins} 枚硬币 (预计命中返还 {est:.1f} 币, {eff_odds:.2f}x)...")
        res = client.place_plaza_bet(predict_id=args.id, opt_idx=args.opt, coins=args.coins)
        if res.get("code") == 0:
            print("下注成功！返回数据:", json.dumps(res.get("data", {}), ensure_ascii=False))
            ledger.record_bet(
                source="plaza",
                predict_id=args.id,
                title=args.title or f"预测广场#{args.id}",
                opt_idx=args.opt,
                opt_desc=args.opt_desc or f"选项{args.opt}",
                coins=args.coins,
                effective_odds=eff_odds,
                true_prob=args.prob,
                ev_ratio=(args.prob * eff_odds - 1.0) if args.prob > 0 and eff_odds > 0 else 0.0,
                expected_return=est,
                reason=args.reason,
            )
        else:
            print(f"下注失败 (code={res.get('code')}): {res.get('message')}")
            sys.exit(1)

    elif args.source == "esports":
        if not args.qid or not args.detail_id:
            print("错误: esports 下注需要提供 --id (contest_id), --qid (question_id), --detail-id (detail_id)")
            sys.exit(1)
        print(f"正在向【赛事中心 cid={args.id} qid={args.qid}】选项 detail_id={args.detail_id} 投入 {args.coins} 枚硬币...")
        res = client.place_esports_bet(
            contest_id=args.id,
            question_id=args.qid,
            detail_id=args.detail_id,
            coins=args.coins,
        )
        if res.get("code") == 0:
            print("赛事中心下注成功！")
            ledger.record_bet(
                source="esports",
                predict_id=args.qid,
                title=args.title or f"赛事#{args.id}_Q{args.qid}",
                opt_idx=args.detail_id,
                opt_desc=args.opt_desc or str(args.detail_id),
                coins=args.coins,
                effective_odds=args.odds,
                true_prob=args.prob,
                ev_ratio=(args.prob * args.odds - 1.0) if args.prob > 0 and args.odds > 0 else 0.0,
                expected_return=args.coins * args.odds,
                reason=args.reason,
            )
        else:
            print(f"赛事中心下注失败 (code={res.get('code')}): {res.get('message')}")
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="B站全品类预测 (预测广场 + 赛事中心) AI 量化投顾 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="扫码登录或导入 Cookie")
    p_login.add_argument("--cookie", type=str, default="", help="直接粘贴浏览器 Cookie 字符串")
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="查看当前账户余额、预测概览与本地下注账本")
    p_status.set_defaults(func=cmd_status)

    p_scan = sub.add_parser("scan", help="扫描预测广场(游戏/体育/文娱/科技/二次元/生活)与传统赛事盘口")
    p_scan.add_argument(
        "--category",
        choices=["all", "by-tab", "game", "sport", "ent", "tech", "acg", "life", "other"],
        default="by-tab",
        help="分类筛选 (by-tab 会逐个扫描全部分类并自动打上分类标签)",
    )
    p_scan.add_argument("--no-expand", action="store_true", help="不自动展开专题内的子盘口")
    p_scan.add_argument("--with-odds", action="store_true", help="调用 /predict/calc 实测每个选项的10硬币真实赔率")
    p_scan.add_argument("--include-esports", action="store_true", help="同时扫描传统赛事中心 (/x/esports/guess)")
    p_scan.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    p_scan.set_defaults(func=cmd_scan)

    p_calc = sub.add_parser("calc", help="对指定盘口进行 +EV 与分数凯利最优硬币数精算")
    p_calc.add_argument("--id", type=int, required=True, help="预测广场题目 predict_id")
    p_calc.add_argument("--probs", type=str, required=True, help="AI评估的各选项真实概率, 格式如 '1:0.65,2:0.35'")
    p_calc.add_argument("--title", type=str, default="", help="题目标题备注")
    p_calc.add_argument("--bankroll", type=float, default=0.0, help="用于凯利计算的硬币本金池 (默认自动读余额或500)")
    p_calc.add_argument("--kelly", type=float, default=0.25, help="凯利保守系数 (默认 0.25 即 1/4 凯利)")
    p_calc.add_argument("--max-bet", type=int, default=100, help="单笔最大硬币上限")
    p_calc.set_defaults(func=cmd_calc)

    p_bet = sub.add_parser("bet", help="提交硬币下注并记录到本地 SQLite 账本")
    p_bet.add_argument("--source", choices=["plaza", "esports"], default="plaza", help="盘口来源")
    p_bet.add_argument("--id", type=int, required=True, help="plaza为predict_id，esports为contest_id")
    p_bet.add_argument("--opt", type=int, default=1, help="plaza选项序号 opt_idx (如 1, 2)")
    p_bet.add_argument("--qid", type=int, default=0, help="esports专用: question_id")
    p_bet.add_argument("--detail-id", type=int, default=0, help="esports专用: detail_id")
    p_bet.add_argument("--coins", type=int, required=True, help="下注硬币数量")
    p_bet.add_argument("--title", type=str, default="", help="盘口标题")
    p_bet.add_argument("--opt-desc", type=str, default="", help="选项名称")
    p_bet.add_argument("--prob", type=float, default=0.0, help="AI评估胜率")
    p_bet.add_argument("--odds", type=float, default=0.0, help="esports赔率")
    p_bet.add_argument("--reason", type=str, default="", help="下注理由记录")
    p_bet.set_defaults(func=cmd_bet)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
