import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class OptionAnalysis:
    opt_idx: int
    opt_desc: str
    crowd_percent: float
    true_prob: float
    effective_odds_10: float  # Effective decimal odds at 10 coins via /predict/calc
    ev_ratio: float           # p_true * odds - 1
    edge_pp: float            # (p_true - implied_prob) * 100
    recommended_coins: int
    expected_return_coins: float
    expected_net_profit: float
    effective_odds_at_rec: float
    tier: str                 # STRONG (+EV>=15%), VALUE (+EV>=8%), WATCH (+EV>0), PASS (<=0)
    note: str


def parse_pool_coins(display_text: str) -> float:
    """Extracts pool size in coins from strings like '硬币奖池：6139.5万　11月15日 00:00封盘'."""
    if not display_text:
        return 0.0
    import re
    m = re.search(r"奖池[：:]\s*([0-9.]+)\s*(万|亿)?", display_text)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        val *= 10000.0
    elif unit == "亿":
        val *= 100000000.0
    return val


def fractional_kelly_fixed_odds(
    true_prob: float,
    decimal_odds: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_bet_ratio: float = 0.10,
    max_single_bet: int = 200,
) -> int:
    """
    Standard Fractional Kelly for fixed or deep-pool odds:
      f* = alpha * (p * b - q) / b, where b = decimal_odds - 1
    """
    if decimal_odds <= 1.02 or true_prob <= 0.0 or true_prob >= 1.0 or bankroll < 1.0:
        return 0
    b = decimal_odds - 1.0
    q = 1.0 - true_prob
    raw_f = (true_prob * b - q) / b
    if raw_f <= 0.0:
        return 0
    f = min(raw_f * kelly_fraction, max_bet_ratio)
    coins = int(round(bankroll * f))
    if coins < 1 and raw_f >= 0.08 and bankroll >= 10:
        coins = 1
    return min(coins, max_single_bet, int(bankroll))


def optimize_plaza_bet_with_calc(
    calc_fn,
    predict_id: int,
    opt_idx: int,
    true_prob: float,
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_bet_ratio: float = 0.10,
    max_single_bet: int = 200,
) -> Tuple[int, float, float]:
    """
    Uses Bilibili's live `/x/act-community/predict/calc` endpoint to account for:
    1) Early/late time-weight bonus in Prediction Plaza
    2) Self-dilution in smaller coin pools when increasing bet size
    Returns (best_coins, predicted_payout, effective_odds).
    """
    # Base probe at 10 coins to check baseline effective odds
    pred_10 = calc_fn(predict_id, opt_idx, 10)
    odds_10 = pred_10 / 10.0 if pred_10 > 0 else 0.0
    if odds_10 <= 1.02:
        return 0, 0.0, odds_10

    ev_10 = true_prob * odds_10 - 1.0
    if ev_10 < 0.05:  # Require at least +5% EV edge to bet
        return 0, 0.0, odds_10

    # Candidate bet sizes bounded by bankroll and max_single_bet
    cap = min(max_single_bet, max(1, int(bankroll * max_bet_ratio)), int(bankroll))
    baseline_kelly = fractional_kelly_fixed_odds(
        true_prob=true_prob,
        decimal_odds=odds_10,
        bankroll=bankroll,
        kelly_fraction=kelly_fraction,
        max_bet_ratio=max_bet_ratio,
        max_single_bet=cap,
    )
    if baseline_kelly <= 0:
        return 0, 0.0, odds_10

    candidates = sorted(
        set(
            c
            for c in [1, 2, 5, 10, 15, 20, 30, 50, 80, 100, 150, 200, baseline_kelly]
            if 1 <= c <= cap
        )
    )

    best_c = 0
    best_pred = 0.0
    best_odds = odds_10
    best_utility = 0.0
    eff_bankroll = max(bankroll / max(kelly_fraction, 0.05), float(cap * 2))

    for c in candidates:
        pred = pred_10 if c == 10 else calc_fn(predict_id, opt_idx, c)
        odds = pred / float(c) if pred > 0 else 0.0
        if odds <= 1.02:
            continue
        ev = true_prob * odds - 1.0
        if ev < 0.05:
            continue
        # Fractional Kelly log-wealth utility
        win_delta = c * (odds - 1.0)
        if c >= eff_bankroll:
            continue
        u = true_prob * math.log(1.0 + win_delta / eff_bankroll) + (1.0 - true_prob) * math.log(
            1.0 - c / eff_bankroll
        )
        if u > best_utility:
            best_utility = u
            best_c = c
            best_pred = pred
            best_odds = odds

    return best_c, best_pred, best_odds


def evaluate_prediction_options(
    predict_id: int,
    options: List[Dict],
    true_probs: Dict[int, float],
    calc_fn,
    bankroll: float = 500.0,
    kelly_fraction: float = 0.25,
    max_single_bet: int = 100,
) -> List[OptionAnalysis]:
    """
    Evaluates all options of a prediction item given AI-estimated true probabilities.
    """
    results: List[OptionAnalysis] = []
    for opt in options:
        idx = int(opt["opt_idx"])
        desc = str(opt.get("opt_desc") or opt.get("option") or f"Option {idx}")
        pct = float(opt.get("percent") or 0.0)
        p_true = float(true_probs.get(idx, 0.0))

        pred_10 = calc_fn(predict_id, idx, 10)
        odds_10 = pred_10 / 10.0 if pred_10 > 0 else (100.0 / pct if pct > 0 else 0.0)
        implied_p = (1.0 / odds_10) if odds_10 > 0 else (pct / 100.0)
        ev_10 = p_true * odds_10 - 1.0
        edge_pp = (p_true - implied_p) * 100.0

        rec_coins, rec_pred, rec_odds = optimize_plaza_bet_with_calc(
            calc_fn=calc_fn,
            predict_id=predict_id,
            opt_idx=idx,
            true_prob=p_true,
            bankroll=bankroll,
            kelly_fraction=kelly_fraction,
            max_single_bet=max_single_bet,
        )

        net_profit = rec_coins * (p_true * rec_odds - 1.0) if rec_coins > 0 else 0.0

        if odds_10 <= 1.0:
            tier = "TRAP"
            note = f"时间权重/高占比陷阱：投10币仅返{pred_10:.1f}币（真实赔率{odds_10:.2f}x < 1.0，猜中也亏本！）"
        elif ev_10 >= 0.15 and edge_pp >= 8.0 and rec_coins > 0:
            tier = "STRONG"
            note = f"强力正期望 (+EV {ev_10*100:.1f}%)，公众情绪偏差显著"
        elif ev_10 >= 0.08 and edge_pp >= 5.0 and rec_coins > 0:
            tier = "VALUE"
            note = f"价值盘口 (+EV {ev_10*100:.1f}%)，建议轻仓介入"
        elif ev_10 > 0.0:
            tier = "WATCH"
            note = f"微弱正期望 (+EV {ev_10*100:.1f}%)，安全边际不足，建议观望或卡点"
        else:
            tier = "PASS"
            note = f"负期望值 (EV {ev_10*100:.1f}%)，放弃"

        results.append(
            OptionAnalysis(
                opt_idx=idx,
                opt_desc=desc,
                crowd_percent=pct,
                true_prob=p_true,
                effective_odds_10=odds_10,
                ev_ratio=ev_10,
                edge_pp=edge_pp,
                recommended_coins=rec_coins,
                expected_return_coins=rec_pred,
                expected_net_profit=net_profit,
                effective_odds_at_rec=rec_odds,
                tier=tier,
                note=note,
            )
        )
    return results
