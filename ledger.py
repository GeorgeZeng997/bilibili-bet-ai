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
