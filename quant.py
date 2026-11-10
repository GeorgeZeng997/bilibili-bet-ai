import datetime
import math
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Unicode escapes for Bilibili API Chinese pool/date labels so source stays pure ASCII
_POOL_PREFIX_RE = re.compile(r"\u5956\u6c60[\uff1a:]\s*(\d+(?:\.\d+)?)\s*(\u4e07|\u4ebf)?")
_POOL_RAW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(\u4e07|\u4ebf)?\s*$")
_UNIT_WAN = "\u4e07"  # 10,000
_UNIT_YI = "\u4ebf"   # 100,000,000
_CN_DATE_RE = re.compile(r"(?:(\d{4})\u5e74)?(\d{1,2})\u6708(\d{1,2})\u65e5\s*(\d{1,2}):(\d{2})")


@dataclass
class OptionAnalysis:
    opt_idx: int
    opt_desc: str
    crowd_percent: float
    true_prob: float
    effective_odds_10: float       # Static snapshot decimal odds at 10 coins via /predict/calc
    terminal_odds_10: float        # Drift-discounted projected terminal odds at close
    drift_retention: float         # Excess-odds retention factor in (0, 1]
    ev_ratio: float                # Raw snapshot EV: p_true * effective_odds_10 - 1
    conservative_ev_ratio: float   # Drift-adjusted EV: p_true * terminal_odds_10 - 1
    edge_pp: float                 # (p_true - implied_prob_terminal) * 100
    recommended_coins: int
    expected_return_coins: float   # Conservative terminal expected payout (coins)
    expected_net_profit: float     # Conservative expected net profit
    effective_odds_at_rec: float   # Drift-adjusted effective odds at recommended stake
    tier: str                      # STRONG, VALUE, SNIPE_LATER, WATCH, TRAP, PASS
    note: str


def parse_pool_coins(display_text: str) -> float:
    """
    Extracts pool size in coins from Bilibili display strings (including 10k / 100M suffixes
    or plain numeric strings like '3580').
    """
    if not display_text:
        return 0.0
    cleaned = str(display_text).replace(",", "")
    m = _POOL_PREFIX_RE.search(cleaned)
    if not m:
        m = _POOL_RAW_RE.match(cleaned)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == _UNIT_WAN:
        val *= 10000.0
    elif unit == _UNIT_YI:
        val *= 100000000.0
    return val


def parse_hours_to_close(text: str, now_ts: Optional[float] = None) -> Optional[float]:
    """
    Parses Bilibili close-time strings (localized month/day strings, ISO '2026-11-15 00:00:00',
    or Unix epoch seconds/milliseconds) into remaining hours until market close.
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    now_epoch = now_ts if now_ts is not None else time.time()
    now_dt = datetime.datetime.fromtimestamp(now_epoch)

    if re.fullmatch(r"\d{9,13}", raw):
        ts_val = float(raw)
        if ts_val > 1e11:
            ts_val /= 1000.0
        delta_hours = (ts_val - now_epoch) / 3600.0
        return max(0.0, round(delta_hours, 2))

    m_iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if m_iso:
        year, month, day, hour, minute = (int(m_iso.group(i)) for i in range(1, 6))
        sec = int(m_iso.group(6) or 0)
        try:
            close_dt = datetime.datetime(year, month, day, hour, minute, sec)
        except ValueError:
            return None
        delta_hours = (close_dt - now_dt).total_seconds() / 3600.0
        return max(0.0, round(delta_hours, 2))

    m = _CN_DATE_RE.search(raw)
    if not m:
        return None

    year_str, month_str, day_str, hour_str, minute_str = m.groups()
    month = int(month_str)
    day = int(day_str)
    hour = int(hour_str)
    minute = int(minute_str)

    if year_str:
        year = int(year_str)
    else:
        year = now_dt.year
        # Handle year rollover (e.g., scanning in Nov/Dec for a Jan/Mar close date)
        if month < now_dt.month - 1:
            year += 1

    try:
        close_dt = datetime.datetime(year, month, day, hour, minute)
    except ValueError:
        return None

    delta_hours = (close_dt - now_dt).total_seconds() / 3600.0
    return max(0.0, round(delta_hours, 2))


def compute_odds_drift_retention(
    pool_coins: float,
    hours_to_close: Optional[float],
) -> Tuple[float, str]:
    """
    Estimates how much of the current excess parimutuel payout `(O_eff - 1)` is likely
    to survive until market close (terminal odds-drift discounting), accounting for:
      1) Shallow-pool dilution when rational bettors arbitrage cognitive mispricings
      2) Late retail inflow & sentiment shifts on distant markets (>24h before close)
    """
    reasons: List[str] = []

    if pool_coins <= 0:
        pool_factor = 0.85
    elif pool_coins < 5000:
        pool_factor = 0.50
        reasons.append(f"very shallow pool ({pool_coins:.0f} coins) vulnerable to arbitrage dilution")
    elif pool_coins < 30000:
        pool_factor = 0.68
        reasons.append(f"shallow pool ({pool_coins:.0f} coins) subject to terminal dilution")
    elif pool_coins < 200000:
        pool_factor = 0.84
    elif pool_coins < 1000000:
        pool_factor = 0.93
    else:
        pool_factor = 1.00

    if hours_to_close is None:
        time_factor = 0.85
    elif hours_to_close <= 6.0:
        time_factor = 0.98
        reasons.append(f"prime late-sniping window ({hours_to_close:.1f}h), pool mature")
    elif hours_to_close <= 12.0:
        time_factor = 0.93
    elif hours_to_close <= 24.0:
        time_factor = 0.85
    elif hours_to_close <= 72.0:
        time_factor = 0.72
        reasons.append(f"{hours_to_close:.1f}h until close, pool still maturing")
    else:
        time_factor = 0.58
        reasons.append(f"distant market ({hours_to_close:.1f}h to close), high terminal odds drift")

    retention = round(max(0.35, min(1.0, pool_factor * time_factor)), 3)
    return retention, "; ".join(reasons)


def apply_drift_haircut(raw_odds: float, retention: float) -> float:
    """Discounts excess decimal odds `(raw_odds - 1)` by `retention` while preserving <=1.0x traps."""
    if raw_odds <= 1.0 or retention >= 0.999:
        return raw_odds
    return 1.0 + (raw_odds - 1.0) * retention


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
    if not all(
        math.isfinite(x)
        for x in (true_prob, decimal_odds, bankroll, kelly_fraction, max_bet_ratio)
    ):
        return 0
    if (
        decimal_odds <= 1.02
        or true_prob <= 0.0
        or true_prob > 1.0
        or bankroll < 1.0
        or kelly_fraction <= 0.0
        or max_bet_ratio <= 0.0
        or max_single_bet <= 0
    ):
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
    drift_retention: float = 1.0,
) -> Tuple[int, float, float]:
    """
    Uses Bilibili's live `/x/act-community/predict/calc` endpoint plus parimutuel
    drift retention (`drift_retention`) to account for:
      1) Early/late time-weight bonus in Prediction Plaza
      2) Self-dilution in smaller coin pools when increasing bet size
      3) Future crowd inflow / smart-money dilution before market close
    Returns (best_coins, conservative_predicted_payout, conservative_effective_odds).
    """
    pred_10 = calc_fn(predict_id, opt_idx, 10)
    raw_odds_10 = pred_10 / 10.0 if pred_10 > 0 else 0.0
    term_odds_10 = apply_drift_haircut(raw_odds_10, drift_retention)
    if (
        not all(math.isfinite(x) for x in (true_prob, bankroll, kelly_fraction, max_bet_ratio))
        or true_prob <= 0.0
        or true_prob > 1.0
        or bankroll < 1.0
        or kelly_fraction <= 0.0
        or max_bet_ratio <= 0.0
        or max_single_bet <= 0
        or term_odds_10 <= 1.02
    ):
        return 0, 0.0, term_odds_10

    ev_10 = true_prob * term_odds_10 - 1.0
    if ev_10 < 0.05:  # Require at least +5% conservative EV edge to bet
        return 0, 0.0, term_odds_10

    cap = min(max_single_bet, max(1, int(bankroll * max_bet_ratio)), int(bankroll))
    baseline_kelly = fractional_kelly_fixed_odds(
        true_prob=true_prob,
        decimal_odds=term_odds_10,
        bankroll=bankroll,
        kelly_fraction=kelly_fraction,
        max_bet_ratio=max_bet_ratio,
        max_single_bet=cap,
    )
    if baseline_kelly <= 0:
        return 0, 0.0, term_odds_10

    candidates = sorted(
        set(
            c
            for c in [1, 2, 5, 10, 15, 20, 30, 50, 80, 100, 150, 200, baseline_kelly]
            if 1 <= c <= cap
        )
    )

    best_c = 0
    best_pred = 0.0
    best_odds = term_odds_10
    best_utility = 0.0
    eff_bankroll = max(bankroll / max(kelly_fraction, 0.05), float(cap * 2))

    for c in candidates:
        raw_pred = pred_10 if c == 10 else calc_fn(predict_id, opt_idx, c)
        raw_odds = raw_pred / float(c) if raw_pred > 0 else 0.0
        odds = apply_drift_haircut(raw_odds, drift_retention)
        if odds <= 1.02:
            continue
        ev = true_prob * odds - 1.0
        if ev < 0.05:
            continue
        win_delta = c * (odds - 1.0)
        if c >= eff_bankroll:
            continue
        u = true_prob * math.log(1.0 + win_delta / eff_bankroll) + (1.0 - true_prob) * math.log(
            1.0 - c / eff_bankroll
        )
        if u > best_utility:
            best_utility = u
            best_c = c
            best_odds = odds
            best_pred = round(c * odds, 2)

    return best_c, best_pred, best_odds


def evaluate_prediction_options(
    predict_id: int,
    options: List[Dict],
    true_probs: Dict[int, float],
    calc_fn,
    bankroll: float = 500.0,
    kelly_fraction: float = 0.25,
    max_single_bet: int = 100,
    pool_coins: float = 0.0,
    hours_to_close: Optional[float] = None,
    ignore_drift: bool = False,
    snipe_window_hours: float = 24.0,
) -> List[OptionAnalysis]:
    """
    Evaluates all options of a prediction item given AI-estimated true probabilities,
    applying parimutuel odds-drift discounting and late-sniping window guards unless
    `ignore_drift=True`.
    """
    if ignore_drift:
        retention, drift_note = 1.0, "odds-drift discounting disabled (--ignore-drift)"
    else:
        retention, drift_note = compute_odds_drift_retention(pool_coins, hours_to_close)

    results: List[OptionAnalysis] = []
    for opt in options:
        idx = int(opt["opt_idx"])
        desc = str(opt.get("opt_desc") or opt.get("option") or f"Option {idx}")
        pct = float(opt.get("percent") or 0.0)
        p_true = float(true_probs.get(idx, 0.0))

        pred_10 = calc_fn(predict_id, idx, 10)
        odds_10 = pred_10 / 10.0 if pred_10 > 0 else (100.0 / pct if pct > 0 else 0.0)
        term_odds_10 = apply_drift_haircut(odds_10, retention)

        implied_p_term = (1.0 / term_odds_10) if term_odds_10 > 0 else (pct / 100.0)
        raw_ev_10 = p_true * odds_10 - 1.0
        cons_ev_10 = p_true * term_odds_10 - 1.0
        edge_pp = (p_true - implied_p_term) * 100.0

        is_too_early = (
            not ignore_drift
            and hours_to_close is not None
            and hours_to_close > snipe_window_hours
        )

        if is_too_early and raw_ev_10 >= 0.08:
            rec_coins, rec_pred, rec_odds = 0, 0.0, term_odds_10
        else:
            rec_coins, rec_pred, rec_odds = optimize_plaza_bet_with_calc(
                calc_fn=calc_fn,
                predict_id=predict_id,
                opt_idx=idx,
                true_prob=p_true,
                bankroll=bankroll,
                kelly_fraction=kelly_fraction,
                max_single_bet=max_single_bet,
                drift_retention=retention,
            )

        net_profit = rec_coins * (p_true * rec_odds - 1.0) if rec_coins > 0 else 0.0
        suffix = f" ({drift_note})" if drift_note else ""

        if odds_10 <= 1.0:
            tier = "TRAP"
            note = (
                f"Time-weight / heavy-favorite trap: 10 coins return {pred_10:.1f} coins "
                f"(effective odds {odds_10:.2f}x <= 1.00, loses coins even if correct)"
            )
        elif is_too_early and raw_ev_10 >= 0.08:
            tier = "SNIPE_LATER"
            note = (
                f"Hold for late snipe: snapshot EV {raw_ev_10*100:+.1f}% "
                f"(discounted {term_odds_10:.2f}x / conservative EV {cons_ev_10*100:+.1f}%), "
                f"{hours_to_close:.1f}h to close — re-probe 3-8h before close"
            )
        elif cons_ev_10 >= 0.15 and edge_pp >= 8.0 and rec_coins > 0:
            tier = "STRONG"
            note = f"Strong +EV (conservative EV {cons_ev_10*100:+.1f}%, snapshot EV {raw_ev_10*100:+.1f}%){suffix}"
        elif cons_ev_10 >= 0.08 and edge_pp >= 5.0 and rec_coins > 0:
            tier = "VALUE"
            note = f"Value edge (conservative EV {cons_ev_10*100:+.1f}%, snapshot EV {raw_ev_10*100:+.1f}%){suffix}"
        elif cons_ev_10 > 0.0 or raw_ev_10 > 0.05:
            tier = "WATCH"
            note = (
                f"Insufficient margin after drift haircut (conservative EV {cons_ev_10*100:+.1f}%, "
                f"snapshot EV {raw_ev_10*100:+.1f}%), re-check near close{suffix}"
            )
        else:
            tier = "PASS"
            note = f"Negative expected value (conservative EV {cons_ev_10*100:+.1f}%), pass"

        results.append(
            OptionAnalysis(
                opt_idx=idx,
                opt_desc=desc,
                crowd_percent=pct,
                true_prob=p_true,
                effective_odds_10=odds_10,
                terminal_odds_10=term_odds_10,
                drift_retention=retention,
                ev_ratio=raw_ev_10,
                conservative_ev_ratio=cons_ev_10,
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
