import contextlib
import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class BetLedger:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (Path(__file__).parent / "data" / "ledger.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    predict_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    opt_idx INTEGER NOT NULL,
                    opt_desc TEXT NOT NULL,
                    coins INTEGER NOT NULL,
                    effective_odds REAL DEFAULT 0.0,
                    true_prob REAL DEFAULT 0.0,
                    ev_ratio REAL DEFAULT 0.0,
                    expected_return REAL DEFAULT 0.0,
                    reason TEXT DEFAULT '',
                    status TEXT DEFAULT 'PENDING',
                    actual_gain REAL DEFAULT 0.0
                )
                """
            )
            conn.commit()

    def record_bet(
        self,
        source: str,
        predict_id: int,
        title: str,
        opt_idx: int,
        opt_desc: str,
        coins: int,
        effective_odds: float = 0.0,
        true_prob: float = 0.0,
        ev_ratio: float = 0.0,
        expected_return: float = 0.0,
        category: str = "",
        reason: str = "",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO bets (
                    created_at, source, predict_id, title, category,
                    opt_idx, opt_desc, coins, effective_odds, true_prob,
                    ev_ratio, expected_return, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time()),
                    source,
                    predict_id,
                    title,
                    category,
                    opt_idx,
                    opt_desc,
                    coins,
                    effective_odds,
                    true_prob,
                    ev_ratio,
                    expected_return,
                    reason,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_bets(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM bets ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def has_pending_bet(self, source: str, predict_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM bets WHERE source = ? AND predict_id = ? AND status = 'PENDING' LIMIT 1",
                (source, predict_id),
            ).fetchone()
            return row is not None

    def update_settlement(
        self,
        predict_id: int,
        status: str,
        actual_gain: float,
        source: Optional[str] = None,
    ) -> int:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if source:
                rows = conn.execute(
                    "SELECT id, coins FROM bets WHERE predict_id = ? AND source = ? ORDER BY id ASC",
                    (predict_id, source),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, coins FROM bets WHERE predict_id = ? ORDER BY id ASC",
                    (predict_id,),
                ).fetchall()

            if not rows:
                return 0

            total_coins = sum(max(0, int(r["coins"] or 0)) for r in rows)
            n = len(rows)
            for r in rows:
                row_coins = max(0, int(r["coins"] or 0))
                if total_coins > 0:
                    allocated_gain = actual_gain * (row_coins / float(total_coins))
                else:
                    allocated_gain = actual_gain / float(n)
                conn.execute(
                    "UPDATE bets SET status = ?, actual_gain = ? WHERE id = ?",
                    (status, round(allocated_gain, 4), int(r["id"])),
                )
            conn.commit()
            return n

    def get_summary(self) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT status, coins, actual_gain FROM bets").fetchall()
        won = sum(1 for r in rows if r["status"] == "WON")
        lost = sum(1 for r in rows if r["status"] == "LOST")
        pending = sum(1 for r in rows if r["status"] == "PENDING")
        pending_coins = sum(int(r["coins"] or 0) for r in rows if r["status"] == "PENDING")
        realized_pnl = sum(float(r["actual_gain"] or 0.0) for r in rows if r["status"] in ("WON", "LOST"))
        return {
            "total_bets": len(rows),
            "won": won,
            "lost": lost,
            "pending": pending,
            "pending_coins": pending_coins,
            "realized_pnl": round(realized_pnl, 2),
        }

    def get_analytics(self) -> Dict[str, Any]:
        """Computes quantitative portfolio metrics: ROI %, Profit Factor, Drawdown, and Brier Calibration."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM bets ORDER BY created_at ASC, id ASC"
            ).fetchall()

        total_bets = len(rows)
        won = 0
        lost = 0
        pending = 0
        pending_coins = 0
        settled_staked = 0
        gross_profit = 0.0
        gross_loss = 0.0
        realized_pnl = 0.0

        cum_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0

        brier_terms: List[float] = []
        by_source: Dict[str, Dict[str, Any]] = {}

        for r in rows:
            st = str(r["status"] or "PENDING")
            coins = max(0, int(r["coins"] or 0))
            gain = float(r["actual_gain"] or 0.0)
            p_true = float(r["true_prob"] or 0.0)
            src = str(r["source"] or "unknown")

            src_bucket = by_source.setdefault(
                src, {"bets": 0, "settled": 0, "staked_coins": 0, "realized_pnl": 0.0}
            )
            src_bucket["bets"] += 1
            src_bucket["staked_coins"] += coins

            if st == "PENDING":
                pending += 1
                pending_coins += coins
                continue

            if st not in ("WON", "LOST"):
                continue

            src_bucket["settled"] += 1
            src_bucket["realized_pnl"] = round(src_bucket["realized_pnl"] + gain, 4)
            settled_staked += coins
            realized_pnl += gain

            if st == "WON":
                won += 1
            else:
                lost += 1

            if gain >= 0.0:
                gross_profit += gain
            else:
                gross_loss += abs(gain)

            cum_pnl += gain
            if cum_pnl > peak_pnl:
                peak_pnl = cum_pnl
            dd = peak_pnl - cum_pnl
            if dd > max_drawdown:
                max_drawdown = dd

            if 0.0 < p_true <= 1.0:
                outcome = 1.0 if st == "WON" else 0.0
                brier_terms.append((p_true - outcome) ** 2)

        settled_bets = won + lost
        win_rate_pct = (won / float(settled_bets) * 100.0) if settled_bets > 0 else 0.0
        roi_pct = (realized_pnl / float(settled_staked) * 100.0) if settled_staked > 0 else 0.0
        profit_factor: Optional[float] = (
            round(gross_profit / gross_loss, 3) if gross_loss > 0.0 else (None if gross_profit == 0.0 else float("inf"))
        )
        brier_score: Optional[float] = (
            round(sum(brier_terms) / float(len(brier_terms)), 4) if brier_terms else None
        )
        current_drawdown = max(0.0, peak_pnl - cum_pnl)

        return {
            "total_bets": total_bets,
            "settled_bets": settled_bets,
            "won": won,
            "lost": lost,
            "pending": pending,
            "pending_coins": pending_coins,
            "settled_staked_coins": settled_staked,
            "win_rate_pct": round(win_rate_pct, 2),
            "realized_pnl": round(realized_pnl, 2),
            "roi_pct": round(roi_pct, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": profit_factor,
            "max_drawdown_coins": round(max_drawdown, 2),
            "current_drawdown_coins": round(current_drawdown, 2),
            "brier_score": brier_score,
            "calibrated_samples": len(brier_terms),
            "by_source": by_source,
        }

    def check_risk_guardrails(
        self,
        proposed_coins: int,
        bankroll: float = 0.0,
        max_exposure_pct: float = 0.35,
        max_drawdown_coins: float = 0.0,
        source: Optional[str] = None,
        predict_id: Optional[int] = None,
        prevent_duplicate_pending: bool = False,
    ) -> Dict[str, Any]:
        """Evaluates portfolio risk caps (active exposure concentration, drawdown breaker, duplicate position)."""
        analytics = self.get_analytics()
        pending_coins = int(analytics["pending_coins"])
        current_dd = float(analytics["current_drawdown_coins"])
        reasons: List[str] = []

        if prevent_duplicate_pending and source and predict_id is not None:
            if self.has_pending_bet(source, predict_id):
                reasons.append(
                    f"Duplicate PENDING position detected for {source}#{predict_id} (omit --no-duplicate to override)."
                )

        projected_exposure_pct = 0.0
        if bankroll > 0.0:
            # Note: live coin balance already excludes pending_coins deducted by Bilibili, so total bankroll at risk
            # equals (live_balance + pending_coins). Exposure ratio = (pending_coins + proposed_coins) / total_equity.
            total_equity = bankroll + pending_coins
            projected_exposure_pct = (pending_coins + proposed_coins) / float(total_equity) if total_equity > 0 else 1.0
            if proposed_coins > bankroll:
                reasons.append(
                    f"Proposed stake ({proposed_coins} coins) exceeds available balance ({bankroll:.1f} coins)."
                )
            elif max_exposure_pct > 0.0 and projected_exposure_pct > max_exposure_pct + 1e-9:
                reasons.append(
                    f"Projected active exposure {(projected_exposure_pct * 100.0):.1f}% "
                    f"({pending_coins + proposed_coins}/{total_equity:.1f} coins) exceeds cap {(max_exposure_pct * 100.0):.1f}%."
                )

        if max_drawdown_coins > 0.0 and current_dd >= max_drawdown_coins:
            reasons.append(
                f"Drawdown circuit breaker active: current drawdown {current_dd:.1f} coins >= limit {max_drawdown_coins:.1f} coins."
            )

        return {
            "allowed": len(reasons) == 0,
            "reasons": reasons,
            "pending_coins": pending_coins,
            "projected_exposure_pct": round(projected_exposure_pct * 100.0, 2),
            "current_drawdown_coins": current_dd,
        }

    def export_records(self, out_path: Path, fmt: str = "auto") -> int:
        """Exports full betting history to CSV or JSON and returns number of exported rows."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM bets ORDER BY id ASC").fetchall()]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_fmt = fmt.lower().strip()
        if resolved_fmt == "auto":
            resolved_fmt = "json" if out_path.suffix.lower() == ".json" else "csv"

        if resolved_fmt == "json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        elif resolved_fmt == "csv":
            fieldnames = [
                "id",
                "created_at",
                "source",
                "predict_id",
                "title",
                "category",
                "opt_idx",
                "opt_desc",
                "coins",
                "effective_odds",
                "true_prob",
                "ev_ratio",
                "expected_return",
                "reason",
                "status",
                "actual_gain",
            ]
            with out_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row.get(k, "") for k in fieldnames})
        else:
            raise ValueError(f"Unsupported export format '{fmt}'. Expected 'csv' or 'json'.")
        return len(rows)
