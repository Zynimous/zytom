"""SQLite persistence: items, append-only log, observations, prefs, alerts, notes."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import Item, Log, Observation
from .timeutil import now_utc, parse_dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    parent TEXT,
    status TEXT NOT NULL,
    category TEXT,
    external_id TEXT UNIQUE,
    body TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_parent ON items(parent);
CREATE INDEX IF NOT EXISTS items_status ON items(status);

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    item_id TEXT,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_item ON log(item_id);
CREATE INDEX IF NOT EXISTS log_kind ON log(kind);
-- 事件 log 只能追加
CREATE TRIGGER IF NOT EXISTS log_no_update BEFORE UPDATE ON log
BEGIN SELECT RAISE(ABORT, 'log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS log_no_delete BEFORE DELETE ON log
BEGIN SELECT RAISE(ABORT, 'log is append-only'); END;

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    scope TEXT,
    status TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prefs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    severity REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(key);

CREATE TABLE IF NOT EXISTS notes (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    ts TEXT NOT NULL,
    author TEXT NOT NULL,      -- user / agent
    state TEXT NOT NULL,       -- current / superseded / proposed / rejected
    content TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (name, version)
);
"""


def default_db_path() -> Path:
    env = os.environ.get("ZYTOM_DB")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "zytom" / "zytom.db"


def new_id(prefix: str = "i") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Store:
    def __init__(self, path: str | Path | None = None, clock=None):
        self.clock = clock or now_utc
        path = Path(path) if path is not None else default_db_path()
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- items
    def get_item(self, item_id: str) -> Item | None:
        row = self.db.execute("SELECT body FROM items WHERE id=?", (item_id,)).fetchone()
        return Item.model_validate_json(row["body"]) if row else None

    def get_by_external(self, external_id: str) -> Item | None:
        row = self.db.execute(
            "SELECT body FROM items WHERE external_id=?", (external_id,)
        ).fetchone()
        return Item.model_validate_json(row["body"]) if row else None

    def items(self, statuses: Iterable[str] | None = None) -> list[Item]:
        if statuses:
            statuses = list(statuses)
            q = f"SELECT body FROM items WHERE status IN ({','.join('?' * len(statuses))})"
            rows = self.db.execute(q, statuses).fetchall()
        else:
            rows = self.db.execute("SELECT body FROM items").fetchall()
        return [Item.model_validate_json(r["body"]) for r in rows]

    def save_item(self, item: Item, commit: bool = True) -> Item:
        self.db.execute(
            """INSERT INTO items(id, parent, status, category, external_id, body, updated)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET parent=excluded.parent, status=excluded.status,
                 category=excluded.category, external_id=excluded.external_id,
                 body=excluded.body, updated=excluded.updated""",
            (
                item.id,
                item.parent,
                item.status,
                item.category,
                item.meta.get("external_id"),
                item.model_dump_json(by_alias=True),
                item.updated.isoformat(),
            ),
        )
        if commit:
            self.db.commit()
        return item

    # --------------------------------------------------------------- log
    def append(
        self,
        kind: str,
        item_id: str | None = None,
        data: dict[str, Any] | None = None,
        ts: datetime | None = None,
        commit: bool = True,
    ) -> Log:
        log = Log(ts=ts or self.clock(), item_id=item_id, kind=kind, data=data or {})  # type: ignore[arg-type]
        cur = self.db.execute(
            "INSERT INTO log(ts, item_id, kind, data) VALUES (?,?,?,?)",
            (log.ts.isoformat(), item_id, kind, json.dumps(log.data, ensure_ascii=False, default=str)),
        )
        log.id = cur.lastrowid
        if commit:
            self.db.commit()
        return log

    def logs(
        self,
        item_id: str | None = None,
        kinds: Iterable[str] | None = None,
        since: datetime | None = None,
    ) -> list[Log]:
        q, args = "SELECT * FROM log WHERE 1=1", []
        if item_id:
            q += " AND item_id=?"
            args.append(item_id)
        if kinds:
            kinds = list(kinds)
            q += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if since:
            q += " AND ts>=?"
            args.append(since.isoformat())
        q += " ORDER BY id"
        return [
            Log(id=r["id"], ts=parse_dt(r["ts"]), item_id=r["item_id"], kind=r["kind"], data=json.loads(r["data"]))
            for r in self.db.execute(q, args).fetchall()
        ]

    # ------------------------------------------------------ observations
    def observations(self, scope: str | None = None, include_rejected: bool = False) -> list[Observation]:
        rows = self.db.execute("SELECT body FROM observations").fetchall()
        obs = [Observation.model_validate_json(r["body"]) for r in rows]
        if not include_rejected:
            obs = [o for o in obs if o.status != "rejected"]
        if scope:
            obs = [o for o in obs if o.scope in (None, "", "*", scope)]
        return obs

    def save_observation(self, obs: Observation) -> Observation:
        self.db.execute(
            """INSERT INTO observations(id, scope, status, body) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET scope=excluded.scope, status=excluded.status, body=excluded.body""",
            (obs.id, obs.scope, obs.status, obs.model_dump_json()),
        )
        self.db.commit()
        return obs

    # ------------------------------------------------------------- prefs
    def pref(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_pref(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO prefs(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False, default=str)),
        )
        self.db.commit()

    def all_prefs(self) -> dict[str, Any]:
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM prefs")}

    # ------------------------------------------------------------ alerts
    def last_alert(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM alerts WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)
        ).fetchone()
        return dict(row) | {"body": json.loads(row["body"])} if row else None

    def save_alert(self, alert_id: str, key: str, level: str, severity: float, body: dict) -> None:
        self.db.execute(
            "INSERT INTO alerts(id, key, ts, level, severity, body) VALUES (?,?,?,?,?,?)",
            (alert_id, key, self.clock().isoformat(), level, severity, json.dumps(body, ensure_ascii=False, default=str)),
        )
        self.db.commit()

    def alerts(self, undelivered_only: bool = False, limit: int = 50) -> list[dict]:
        q = "SELECT * FROM alerts"
        if undelivered_only:
            q += " WHERE delivered=0"
        q += " ORDER BY ts DESC LIMIT ?"
        return [dict(r) | {"body": json.loads(r["body"])} for r in self.db.execute(q, (limit,))]

    def mark_delivered(self, alert_ids: Iterable[str]) -> None:
        self.db.executemany("UPDATE alerts SET delivered=1 WHERE id=?", [(a,) for a in alert_ids])
        self.db.commit()

    # ------------------------------------------------------------- notes
    def note(self, name: str, state: str = "current") -> dict | None:
        row = self.db.execute(
            "SELECT * FROM notes WHERE name=? AND state=? ORDER BY version DESC LIMIT 1", (name, state)
        ).fetchone()
        return dict(row) if row else None

    def notes(self, state: str = "current") -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM notes WHERE state=? ORDER BY name, version", (state,)
        )]

    def note_history(self, name: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM notes WHERE name=? ORDER BY version", (name,)
        )]

    def add_note_version(self, name: str, content: str, author: str, state: str, reason: str | None = None) -> int:
        row = self.db.execute("SELECT MAX(version) AS v FROM notes WHERE name=?", (name,)).fetchone()
        version = (row["v"] or 0) + 1
        if state == "current":
            self.db.execute("UPDATE notes SET state='superseded' WHERE name=? AND state='current'", (name,))
        self.db.execute(
            "INSERT INTO notes(name, version, ts, author, state, content, reason) VALUES (?,?,?,?,?,?,?)",
            (name, version, self.clock().isoformat(), author, state, content, reason),
        )
        self.db.commit()
        return version

    def set_note_state(self, name: str, version: int, state: str) -> None:
        if state == "current":
            self.db.execute("UPDATE notes SET state='superseded' WHERE name=? AND state='current'", (name,))
        self.db.execute("UPDATE notes SET state=? WHERE name=? AND version=?", (state, name, version))
        self.db.commit()
