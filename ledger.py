import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class BetLedger:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (Path(__file__).parent / "data" / "ledger.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM bets ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def update_settlement(
        self,
        predict_id: int,
        status: str,
        actual_gain: float,
        source: Optional[str] = None,
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            if source:
                cur = conn.execute(
                    "UPDATE bets SET status = ?, actual_gain = ? WHERE predict_id = ? AND source = ?",
                    (status, actual_gain, predict_id, source),
                )
            else:
                cur = conn.execute(
                    "UPDATE bets SET status = ?, actual_gain = ? WHERE predict_id = ?",
                    (status, actual_gain, predict_id),
                )
            conn.commit()
            return int(cur.rowcount or 0)

    def get_summary(self) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
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

