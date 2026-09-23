from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev
from threading import Lock
from zoneinfo import ZoneInfo

from guardian.domain.models import Candle, OrderResult, RiskSnapshot, Signal


class SqliteTradingRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange_order_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    status TEXT NOT NULL,
                    executed_quantity TEXT NOT NULL,
                    quote_quantity TEXT NOT NULL,
                    average_price TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL DEFAULT '0',
                    is_simulated INTEGER NOT NULL CHECK(is_simulated IN (0,1)),
                    reference_price TEXT,
                    fee_quote TEXT NOT NULL DEFAULT '0',
                    slippage_quote TEXT NOT NULL DEFAULT '0',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    fast_sma TEXT,
                    slow_sma TEXT,
                    reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(symbol, interval, open_time)
                );
                CREATE TABLE IF NOT EXISTS training_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_training_generated
                    ON training_runs(generated_at DESC);
                CREATE TABLE IF NOT EXISTS bot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_created
                    ON bot_events(created_at DESC);
                CREATE TABLE IF NOT EXISTS equity_samples (
                    sample_time TEXT PRIMARY KEY,
                    equity TEXT NOT NULL,
                    price TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_models (
                    model_id TEXT PRIMARY KEY,
                    training_generated_at TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    name TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    quote_balance TEXT NOT NULL,
                    base_quantity TEXT NOT NULL DEFAULT '0',
                    entry_cash TEXT NOT NULL DEFAULT '0',
                    entry_price TEXT NOT NULL DEFAULT '0',
                    peak_price TEXT NOT NULL DEFAULT '0',
                    pending_action TEXT,
                    last_candle TEXT,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_models_active
                    ON shadow_models(active, strategy);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_active_slot
                    ON shadow_models(strategy, interval) WHERE active=1;
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL REFERENCES shadow_models(model_id),
                    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                    quantity TEXT NOT NULL,
                    quote_quantity TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    fill_price TEXT NOT NULL,
                    fee_quote TEXT NOT NULL,
                    slippage_quote TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL DEFAULT '0',
                    reason TEXT NOT NULL,
                    candle_time TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_trades_model
                    ON shadow_trades(model_id, candle_time);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_trade_fill
                    ON shadow_trades(model_id, side, candle_time);
                CREATE TABLE IF NOT EXISTS shadow_equity_samples (
                    model_id TEXT NOT NULL REFERENCES shadow_models(model_id),
                    sample_time TEXT NOT NULL,
                    equity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    in_position INTEGER NOT NULL CHECK(in_position IN (0,1)),
                    PRIMARY KEY(model_id, sample_time)
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_equity_time
                    ON shadow_equity_samples(model_id, sample_time);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)")}
            migrations = {
                "reference_price": "ALTER TABLE orders ADD COLUMN reference_price TEXT",
                "fee_quote": "ALTER TABLE orders ADD COLUMN fee_quote TEXT NOT NULL DEFAULT '0'",
                "slippage_quote": (
                    "ALTER TABLE orders ADD COLUMN slippage_quote TEXT NOT NULL DEFAULT '0'"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    db.execute(statement)

    def _cost_basis_before(self, created_at: str) -> tuple[Decimal, Decimal]:
        quantity = Decimal("0")
        cost = Decimal("0")
        with self._connect() as db:
            rows = db.execute(
                """SELECT side, executed_quantity, quote_quantity FROM orders
                WHERE CAST(executed_quantity AS REAL) > 0 AND created_at <= ?
                ORDER BY created_at, id""",
                (created_at,),
            ).fetchall()
        for row in rows:
            filled = Decimal(row["executed_quantity"])
            quote = Decimal(row["quote_quantity"])
            if row["side"] == "BUY":
                quantity += filled
                cost += quote
            elif quantity > 0:
                sold = min(filled, quantity)
                average_cost = cost / quantity
                quantity -= sold
                cost -= average_cost * sold
        return quantity, cost

    def record_order(self, order: OrderResult, realized_pnl: Decimal | None = None) -> bool:
        return self.record_order_with_state(order, {}, realized_pnl)

    def record_order_with_state(
        self,
        order: OrderResult,
        state: dict[str, str],
        realized_pnl: Decimal | None = None,
    ) -> bool:
        created_at = order.created_at.astimezone(UTC).isoformat()
        if realized_pnl is None:
            realized_pnl = Decimal("0")
            if order.side.value == "SELL" and order.executed_quantity > 0:
                held_quantity, held_cost = self._cost_basis_before(created_at)
                if held_quantity > 0:
                    sold = min(order.executed_quantity, held_quantity)
                    allocated_proceeds = (
                        order.cumulative_quote_quantity * sold / order.executed_quantity
                    )
                    realized_pnl = allocated_proceeds - (held_cost / held_quantity) * sold
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO orders
                (exchange_order_id, client_order_id, symbol, side, status, executed_quantity,
                 quote_quantity, average_price, realized_pnl, is_simulated, reference_price,
                 fee_quote, slippage_quote, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order.order_id,
                    order.client_order_id,
                    order.symbol,
                    order.side.value,
                    order.status,
                    str(order.executed_quantity),
                    str(order.cumulative_quote_quantity),
                    str(order.average_price),
                    str(realized_pnl),
                    int(order.is_simulated),
                    str(order.reference_price) if order.reference_price is not None else None,
                    str(order.fee_quote),
                    str(order.slippage_quote),
                    created_at,
                ),
            )
            if cursor.rowcount == 1 and state:
                now = datetime.now(UTC).isoformat()
                db.executemany(
                    """INSERT INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value, updated_at=excluded.updated_at""",
                    [(key, value, now) for key, value in state.items()],
                )
            return cursor.rowcount == 1

    def list_orders(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 500),)
            ).fetchall()
        return [dict(row) for row in rows]

    def risk_snapshot(
        self, day: date, current_price: Decimal, timezone_name: str = "UTC"
    ) -> RiskSnapshot:
        timezone = ZoneInfo(timezone_name)
        local_start = datetime.combine(day, time.min, tzinfo=timezone)
        start = local_start.astimezone(UTC).isoformat()
        end = (local_start + timedelta(days=1)).astimezone(UTC).isoformat()
        with self._connect() as db:
            today = db.execute(
                """SELECT COUNT(*) trades,
                SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END) entries,
                COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) pnl,
                MAX(created_at) last_trade FROM orders WHERE created_at >= ? AND created_at < ?""",
                (start, end),
            ).fetchone()
            buys = db.execute(
                """SELECT COALESCE(SUM(CAST(executed_quantity AS REAL)),0) q
                FROM orders WHERE side='BUY' AND CAST(executed_quantity AS REAL) > 0"""
            ).fetchone()["q"]
            sells = db.execute(
                """SELECT COALESCE(SUM(CAST(executed_quantity AS REAL)),0) q
                FROM orders WHERE side='SELL' AND CAST(executed_quantity AS REAL) > 0"""
            ).fetchone()["q"]
        last = datetime.fromisoformat(today["last_trade"]) if today["last_trade"] else None
        position = max(Decimal(str(buys)) - Decimal(str(sells)), Decimal("0")) * current_price
        return RiskSnapshot(
            realized_pnl_today=Decimal(str(today["pnl"])),
            trades_today=int(today["trades"]),
            position_quote=position,
            emergency_stop=self.get_state("emergency_stop", "false") == "true",
            last_trade_at=last,
            entries_today=int(today["entries"] or 0),
        )

    def set_state(self, key: str, value: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, now),
            )

    def get_state(self, key: str, default: str = "") -> str:
        with self._connect() as db:
            row = db.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_states(self, values: dict[str, str]) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            db.executemany(
                """INSERT INTO bot_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at""",
                [(key, value, now) for key, value in values.items()],
            )

    def record_observation(
        self, symbol: str, interval: str, candle: Candle, signal: Signal
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO market_observations
                (symbol, interval, open_time, open, high, low, close, volume, signal,
                 fast_sma, slow_sma, reason, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    symbol,
                    interval,
                    candle.open_time.astimezone(UTC).isoformat(),
                    str(candle.open),
                    str(candle.high),
                    str(candle.low),
                    str(candle.close),
                    str(candle.volume),
                    signal.action.value,
                    str(signal.fast_sma) if signal.fast_sma is not None else None,
                    str(signal.slow_sma) if signal.slow_sma is not None else None,
                    signal.reason,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def observation_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0])

    def save_training_result(self, result: dict[str, object]) -> None:
        payload = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO training_runs(generated_at, result_json) VALUES (?, ?)",
                (str(result["generated_at"]), payload),
            )

    def latest_training_result(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT result_json FROM training_runs ORDER BY generated_at DESC, id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def create_shadow_model(self, model: dict[str, object], starting_quote: Decimal) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO shadow_models
                (model_id, training_generated_at, strategy, name, interval, parameters_json,
                 quote_balance, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(model["model_id"]),
                    str(model["training_generated_at"]),
                    str(model["strategy"]),
                    str(model["name"]),
                    str(model["interval"]),
                    json.dumps(model["parameters"], separators=(",", ":"), sort_keys=True),
                    str(starting_quote),
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                db.execute(
                    "UPDATE shadow_models SET active=1, updated_at=? WHERE model_id=?",
                    (now, str(model["model_id"])),
                )
            return cursor.rowcount == 1

    def deactivate_shadow_family(self, strategy: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE shadow_models SET active=0, updated_at=? WHERE strategy=? AND active=1",
                (datetime.now(UTC).isoformat(), strategy),
            )

    def deactivate_shadow_slot(self, strategy: str, interval: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE shadow_models SET active=0, updated_at=?
                WHERE strategy=? AND interval=? AND active=1""",
                (datetime.now(UTC).isoformat(), strategy, interval),
            )

    def list_shadow_models(self, active_only: bool = True) -> list[dict[str, object]]:
        query = "SELECT * FROM shadow_models"
        if active_only:
            query += " WHERE active=1"
        query += " ORDER BY created_at"
        with self._connect() as db:
            rows = db.execute(query).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(str(item.pop("parameters_json")))
            result.append(item)
        return result

    def update_shadow_model(self, model_id: str, state: dict[str, object]) -> None:
        allowed = {
            "quote_balance",
            "base_quantity",
            "entry_cash",
            "entry_price",
            "peak_price",
            "pending_action",
            "last_candle",
        }
        if set(state) - allowed:
            raise ValueError("Estado shadow contiene campos no permitidos")
        assignments = ", ".join(f"{key}=?" for key in state)
        values = [None if value is None else str(value) for value in state.values()]
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE shadow_models SET {assignments}, updated_at=? WHERE model_id=?",  # noqa: S608
                (*values, datetime.now(UTC).isoformat(), model_id),
            )

    def record_shadow_trade(
        self, trade: dict[str, object], state: dict[str, object] | None = None
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO shadow_trades
                (model_id, side, quantity, quote_quantity, reference_price, fill_price,
                 fee_quote, slippage_quote, realized_pnl, reason, candle_time, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(trade["model_id"]),
                    str(trade["side"]),
                    str(trade["quantity"]),
                    str(trade["quote_quantity"]),
                    str(trade["reference_price"]),
                    str(trade["fill_price"]),
                    str(trade["fee_quote"]),
                    str(trade["slippage_quote"]),
                    str(trade.get("realized_pnl", "0")),
                    str(trade["reason"]),
                    str(trade["candle_time"]),
                    datetime.now(UTC).isoformat(),
                ),
            )

            # Fill and wallet must survive a restart together, or neither does.
            if state is not None:
                db.execute(
                    """UPDATE shadow_models SET quote_balance=?, base_quantity=?,
                    entry_cash=?, entry_price=?, peak_price=?, pending_action=?, updated_at=?
                    WHERE model_id=?""",
                    tuple(str(state[key]) for key in (
                        "quote_balance", "base_quantity", "entry_cash", "entry_price", "peak_price"
                    )) + (state.get("pending_action"), datetime.now(UTC).isoformat(),
                          str(trade["model_id"])),
                )

    def list_shadow_trades(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT t.*, m.name, m.strategy, m.interval
                FROM shadow_trades t JOIN shadow_models m ON m.model_id=t.model_id
                ORDER BY t.candle_time DESC, t.id DESC LIMIT ?""",
                (min(max(limit, 1), 500),),
            ).fetchall()
        return [dict(row) for row in rows]

    def shadow_performance(self) -> list[dict[str, object]]:
        models = self.list_shadow_models(active_only=False)
        with self._connect() as db:
            rows = db.execute(
                """SELECT model_id,
                SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN side='SELL' AND CAST(realized_pnl AS REAL)>0 THEN 1 ELSE 0 END) wins,
                COALESCE(SUM(CASE WHEN side='SELL' THEN CAST(realized_pnl AS REAL) END),0) pnl,
                COALESCE(SUM(CASE WHEN side='SELL' AND CAST(realized_pnl AS REAL)>0
                    THEN CAST(realized_pnl AS REAL) END),0) gains,
                COALESCE(SUM(CASE WHEN side='SELL' AND CAST(realized_pnl AS REAL)<0
                    THEN -CAST(realized_pnl AS REAL) END),0) losses,
                COALESCE(SUM(CAST(fee_quote AS REAL)),0) fees,
                COALESCE(SUM(CAST(slippage_quote AS REAL)),0) slippage
                FROM shadow_trades GROUP BY model_id"""
            ).fetchall()
            pnl_rows = db.execute(
                """SELECT model_id, realized_pnl FROM shadow_trades
                WHERE side='SELL' ORDER BY model_id, candle_time"""
            ).fetchall()
            equity_rows = db.execute(
                """SELECT model_id, sample_time, equity, price, in_position FROM (
                    SELECT model_id, sample_time, equity, price, in_position,
                    ROW_NUMBER() OVER (PARTITION BY model_id ORDER BY sample_time DESC) row_num
                    FROM shadow_equity_samples
                ) WHERE row_num <= 500 ORDER BY model_id, sample_time"""
            ).fetchall()
        metrics = {str(row["model_id"]): row for row in rows}
        pnls_by_model: dict[str, list[float]] = {}
        for row in pnl_rows:
            pnls_by_model.setdefault(str(row["model_id"]), []).append(float(row["realized_pnl"]))
        curves_by_model: dict[str, list[dict[str, object]]] = {}
        for row in equity_rows:
            curve = curves_by_model.setdefault(str(row["model_id"]), [])
            curve.append(
                {
                    "time": row["sample_time"],
                    "equity": row["equity"],
                    "price": row["price"],
                    "in_position": bool(row["in_position"]),
                }
            )
        result = []
        for model in models:
            row = metrics.get(str(model["model_id"]))
            completed = int(row["completed"] or 0) if row else 0
            wins = int(row["wins"] or 0) if row else 0
            gains = Decimal(str(row["gains"] or 0)) if row else Decimal("0")
            losses = Decimal(str(row["losses"] or 0)) if row else Decimal("0")
            pnl = Decimal(str(row["pnl"] or 0)) if row else Decimal("0")
            trade_pnls = pnls_by_model.get(str(model["model_id"]), [])
            deviation = pstdev(trade_pnls) if len(trade_pnls) > 1 else 0.0
            downside = [min(value, 0.0) for value in trade_pnls]
            downside_deviation = (
                math.sqrt(mean(value * value for value in downside)) if downside else 0.0
            )
            sharpe = mean(trade_pnls) / deviation if deviation else 0.0
            sortino = mean(trade_pnls) / downside_deviation if downside_deviation else 0.0
            curve = curves_by_model.get(str(model["model_id"]), [])
            peak = Decimal(str(curve[0]["equity"])) if curve else Decimal("0")
            max_drawdown = Decimal("0")
            exposed = 0
            for sample in curve:
                equity = Decimal(str(sample["equity"]))
                peak = max(peak, equity)
                if peak > 0:
                    max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
                exposed += int(bool(sample["in_position"]))
            benchmark = Decimal("0")
            if len(curve) > 1 and Decimal(str(curve[0]["price"])) > 0:
                benchmark = (
                    Decimal(str(curve[-1]["price"])) / Decimal(str(curve[0]["price"])) - 1
                ) * 100
            created = datetime.fromisoformat(str(model["created_at"]))
            age_hours = max(0.0, (datetime.now(UTC) - created).total_seconds() / 3600)
            item = dict(model)
            item.update(
                {
                    "completed_trades": completed,
                    "win_rate_pct": round(wins / completed * 100, 2) if completed else 0.0,
                    "realized_pnl": str(pnl),
                    "profit_factor": str(
                        gains / losses if losses else (Decimal("99") if gains else Decimal("0"))
                    ),
                    "fees": str(Decimal(str(row["fees"] or 0)) if row else Decimal("0")),
                    "slippage": str(Decimal(str(row["slippage"] or 0)) if row else Decimal("0")),
                    "age_hours": round(age_hours, 1),
                    "sharpe_per_trade": round(sharpe, 4),
                    "sortino_per_trade": round(sortino, 4),
                    "max_drawdown_pct": str(max_drawdown),
                    "buy_hold_return_pct": str(benchmark),
                    "exposure_pct": round(exposed / len(curve) * 100, 2) if curve else 0.0,
                    "equity_curve": curve[-500:],
                }
            )
            result.append(item)
        return result

    def record_shadow_equity(
        self,
        model_id: str,
        sample_time: str,
        equity: Decimal,
        price: Decimal,
        in_position: bool,
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO shadow_equity_samples
                (model_id, sample_time, equity, price, in_position) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(model_id, sample_time) DO UPDATE SET
                    equity=excluded.equity, price=excluded.price,
                    in_position=excluded.in_position""",
                (model_id, sample_time, str(equity), str(price), int(in_position)),
            )

    def record_event(
        self,
        event_type: str,
        message: str,
        level: str = "INFO",
        details: dict | None = None,
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO bot_events(event_type, level, message, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    event_type[:40],
                    level[:10],
                    message[:500],
                    json.dumps(details or {}, separators=(",", ":"), ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_events(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT event_type, level, message, details_json, created_at
                FROM bot_events ORDER BY created_at DESC LIMIT ?""",
                (min(max(limit, 1), 500),),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "level": row["level"],
                "message": row["message"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_equity(self, equity: Decimal, price: Decimal) -> None:
        now = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO equity_samples(sample_time, equity, price) VALUES (?, ?, ?)
                ON CONFLICT(sample_time) DO UPDATE SET equity=excluded.equity,
                    price=excluded.price""",
                (now, str(equity), str(price)),
            )

    def performance_summary(self) -> dict[str, object]:
        with self._connect() as db:
            samples = db.execute(
                "SELECT sample_time, equity, price FROM equity_samples ORDER BY sample_time"
            ).fetchall()
            trades = db.execute(
                """SELECT COUNT(*) count,
                SUM(CASE WHEN side='SELL' AND CAST(realized_pnl AS REAL) > 0
                    THEN 1 ELSE 0 END) wins,
                SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) exits,
                COALESCE(SUM(CAST(realized_pnl AS REAL)),0) pnl,
                COALESCE(SUM(CAST(fee_quote AS REAL)),0) fees,
                COALESCE(SUM(CAST(slippage_quote AS REAL)),0) slippage FROM orders"""
            ).fetchone()
            completed = db.execute(
                """SELECT CAST(realized_pnl AS REAL) pnl FROM orders
                WHERE side='SELL' ORDER BY created_at"""
            ).fetchall()
        if not samples:
            return {
                "samples": 0,
                "return_pct": 0,
                "max_drawdown_pct": 0,
                "buy_hold_return_pct": 0,
                "trades": int(trades["count"] or 0),
                "win_rate_pct": 0,
                "completed_trades": 0,
                "expectancy": "0",
                "profit_factor": "0",
                "excess_return_pct": "0",
                "fees": "0",
                "slippage_cost": "0",
                "equity_curve": [],
            }
        equities = [Decimal(row["equity"]) for row in samples]
        prices = [Decimal(row["price"]) for row in samples]
        peak = equities[0]
        drawdown = Decimal("0")
        for equity in equities:
            peak = max(peak, equity)
            if peak > 0:
                drawdown = max(drawdown, (peak - equity) / peak * 100)
        exits = int(trades["exits"] or 0)
        wins = int(trades["wins"] or 0)
        pnls = [Decimal(str(row["pnl"])) for row in completed]
        gains = sum((value for value in pnls if value > 0), Decimal("0"))
        losses = abs(sum((value for value in pnls if value < 0), Decimal("0")))
        expectancy = sum(pnls, Decimal("0")) / Decimal(len(pnls)) if pnls else Decimal("0")
        profit_factor = gains / losses if losses else (Decimal("99") if gains else Decimal("0"))
        return {
            "samples": len(samples),
            "start_time": samples[0]["sample_time"],
            "return_pct": str((equities[-1] / equities[0] - 1) * 100),
            "max_drawdown_pct": str(drawdown),
            "buy_hold_return_pct": str((prices[-1] / prices[0] - 1) * 100),
            "trades": int(trades["count"] or 0),
            "win_rate_pct": str(Decimal(wins) / Decimal(exits) * 100) if exits else "0",
            "realized_pnl": str(trades["pnl"] or 0),
            "fees": str(trades["fees"] or 0),
            "slippage_cost": str(trades["slippage"] or 0),
            "completed_trades": len(pnls),
            "expectancy": str(expectancy),
            "profit_factor": str(profit_factor),
            "excess_return_pct": str(
                (equities[-1] / equities[0] - 1) * 100 - (prices[-1] / prices[0] - 1) * 100
            ),
            "equity_curve": [
                {"time": row["sample_time"], "equity": row["equity"], "price": row["price"]}
                for row in samples[-500:]
            ],
        }
