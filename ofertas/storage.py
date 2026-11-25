"""SQLite persistence (stdlib sqlite3). Schema migrations via PRAGMA user_version."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ofertas.models import RawOffer, StoredOffer, Verdict
from ofertas.normalize import canonical_url, normalize_store, normalize_title, product_key

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE offers (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        url TEXT NOT NULL,
        url_canon TEXT NOT NULL,
        product_key TEXT NOT NULL,
        title TEXT NOT NULL,
        title_norm TEXT NOT NULL,
        category TEXT,
        price REAL NOT NULL,
        list_price REAL,
        store TEXT,
        store_norm TEXT,
        coupon TEXT,
        free_shipping INTEGER,
        votes INTEGER,
        temperature REAL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        UNIQUE (source, source_id)
    );
    CREATE INDEX offers_product ON offers (product_key);

    CREATE TABLE price_history (
        id INTEGER PRIMARY KEY,
        product_key TEXT NOT NULL,
        store_norm TEXT,
        price REAL NOT NULL,
        source TEXT NOT NULL,
        seen_at TEXT NOT NULL
    );
    CREATE INDEX price_history_product ON price_history (product_key, seen_at);

    CREATE TABLE sent (
        id INTEGER PRIMARY KEY,
        offer_id INTEGER NOT NULL REFERENCES offers(id),
        product_key TEXT NOT NULL,
        url_canon TEXT NOT NULL,
        price REAL NOT NULL,
        sent_at TEXT NOT NULL,
        message_id INTEGER
    );
    CREATE INDEX sent_product ON sent (product_key, sent_at);
    CREATE INDEX sent_url ON sent (url_canon, sent_at);

    CREATE TABLE verdicts (
        id INTEGER PRIMARY KEY,
        offer_id INTEGER NOT NULL REFERENCES offers(id),
        price REAL NOT NULL,
        score INTEGER,
        is_fake INTEGER,
        flags_json TEXT,
        category TEXT,
        name_norm TEXT,
        reason TEXT,
        model TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (offer_id, price)
    );

    CREATE TABLE usage (
        day TEXT PRIMARY KEY,
        claude_calls INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        limit_alerted INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE source_health (
        source TEXT PRIMARY KEY,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_ok TEXT,
        alerted INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # v2: Claude's condition judgement (novo/usado/...)
    "ALTER TABLE verdicts ADD COLUMN condition TEXT",
    # v3: what we sent (to edit it when the deal ends) and the user's 👍/👎
    """
    ALTER TABLE sent ADD COLUMN text TEXT;
    ALTER TABLE sent ADD COLUMN photo INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sent ADD COLUMN ended_at TEXT;
    CREATE TABLE feedback (
        offer_id INTEGER PRIMARY KEY REFERENCES offers(id),
        vote INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
]

# A new price-history point is recorded when the price changes, or at most
# once per this interval when it stays the same (keeps the median honest
# without one row per 20-minute run).
HISTORY_REFRESH = timedelta(hours=24)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # -- schema / transactions -------------------------------------------
    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i}; COMMIT;")

    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # -- offers -----------------------------------------------------------
    def upsert_offer(self, raw: RawOffer, now: datetime | None = None) -> StoredOffer:
        now_s = _iso(now or utcnow())
        url_canon = canonical_url(raw.url)
        pkey = product_key(raw.title)
        store_norm = normalize_store(raw.store)
        row = self.conn.execute(
            "SELECT id, price, category FROM offers WHERE source = ? AND source_id = ?",
            (raw.source, raw.source_id),
        ).fetchone()
        fields = dict(
            url=raw.url, url_canon=url_canon, product_key=pkey, title=raw.title,
            title_norm=normalize_title(raw.title), price=raw.price,
            list_price=raw.list_price, store=raw.store, store_norm=store_norm,
            coupon=raw.coupon,
            free_shipping=None if raw.free_shipping is None else int(raw.free_shipping),
            votes=raw.votes, temperature=raw.temperature, last_seen=now_s,
            raw_json=json.dumps(asdict(raw), ensure_ascii=False),
        )
        if row is None:
            cols = ["source", "source_id", "first_seen", *fields]
            vals = [raw.source, raw.source_id, now_s, *fields.values()]
            cur = self.conn.execute(
                f"INSERT INTO offers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            offer_id, is_new, changed, category = cur.lastrowid, True, True, None
        else:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(
                f"UPDATE offers SET {sets} WHERE id = ?", [*fields.values(), row["id"]]
            )
            offer_id, is_new = row["id"], False
            changed, category = abs(row["price"] - raw.price) >= 0.01, row["category"]
        self._record_price(pkey, store_norm, raw.price, raw.source, now or utcnow())
        return StoredOffer(
            id=offer_id, raw=raw, url_canon=url_canon, product_key=pkey,
            is_new=is_new, price_changed=changed, category=category,
        )

    def _record_price(self, pkey, store_norm, price, source, now: datetime) -> None:
        last = self.conn.execute(
            "SELECT price, seen_at FROM price_history WHERE product_key = ? "
            "AND store_norm IS ? ORDER BY seen_at DESC LIMIT 1",
            (pkey, store_norm),
        ).fetchone()
        if last and abs(last["price"] - price) < 0.01:
            if now - datetime.fromisoformat(last["seen_at"]) < HISTORY_REFRESH:
                return
        self.conn.execute(
            "INSERT INTO price_history (product_key, store_norm, price, source, seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pkey, store_norm, price, source, _iso(now)),
        )

    def price_history(self, pkey: str, since: datetime, before: datetime | None = None) -> list[float]:
        """Prices seen for a product in [since, before). `before` excludes the
        current run's own observation from the baseline."""
        sql = "SELECT price FROM price_history WHERE product_key = ? AND seen_at >= ?"
        args: list = [pkey, _iso(since)]
        if before is not None:
            sql += " AND seen_at < ?"
            args.append(_iso(before))
        return [r["price"] for r in self.conn.execute(sql + " ORDER BY seen_at", args)]

    # -- sent / dedupe ----------------------------------------------------
    def last_sent(self, pkey: str, url_canon: str, since: datetime) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT price, sent_at FROM sent WHERE (product_key = ? OR url_canon = ?) "
            "AND sent_at >= ? ORDER BY price ASC LIMIT 1",
            (pkey, url_canon, _iso(since)),
        ).fetchone()

    def recent_sent(self, since: datetime) -> list[sqlite3.Row]:
        """Sent rows with the offer's store, for variant (same family) checks."""
        return self.conn.execute(
            "SELECT s.product_key, s.price, o.store_norm FROM sent s "
            "LEFT JOIN offers o ON o.id = s.offer_id WHERE s.sent_at >= ?",
            (_iso(since),),
        ).fetchall()

    def recent_sent_names(self, since: datetime) -> list[sqlite3.Row]:
        """Claude's normalised name and price of offers sent since `since`:
        the same product posted again with another title/source/offer id."""
        return self.conn.execute(
            "SELECT v.name_norm, s.price FROM sent s JOIN verdicts v "
            "ON v.offer_id = s.offer_id AND abs(v.price - s.price) < 0.01 "
            "WHERE s.sent_at >= ? AND v.name_norm IS NOT NULL",
            (_iso(since),),
        ).fetchall()

    def mark_sent(self, offer: StoredOffer, message_id: int | None, now: datetime | None = None,
                  text: str | None = None, photo: bool = False):
        self.conn.execute(
            "INSERT INTO sent (offer_id, product_key, url_canon, price, sent_at, message_id, "
            "text, photo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (offer.id, offer.product_key, offer.url_canon, offer.raw.price,
             _iso(now or utcnow()), message_id, text, int(photo)),
        )

    def open_sent_for(self, keys: set[tuple[str, str]], since: datetime) -> list[sqlite3.Row]:
        """Sent messages (not yet marked ended) for these (source, source_id)."""
        if not keys:
            return []
        rows = self.conn.execute(
            "SELECT s.id, s.offer_id, s.message_id, s.text, s.photo, o.source, o.source_id, o.url "
            "FROM sent s JOIN offers o ON o.id = s.offer_id "
            "WHERE s.ended_at IS NULL AND s.message_id IS NOT NULL AND s.text IS NOT NULL "
            "AND s.sent_at >= ?", (_iso(since),),
        ).fetchall()
        return [r for r in rows if (r["source"], r["source_id"]) in keys]

    def mark_ended(self, sent_id: int, now: datetime | None = None) -> None:
        self.conn.execute("UPDATE sent SET ended_at = ? WHERE id = ?", (_iso(now or utcnow()), sent_id))

    # -- user feedback (👍/👎) ----------------------------------------------
    def set_feedback(self, offer_id: int, vote: int, now: datetime | None = None) -> None:
        self.conn.execute(
            "INSERT INTO feedback (offer_id, vote, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(offer_id) DO UPDATE SET vote = excluded.vote, created_at = excluded.created_at",
            (offer_id, vote, _iso(now or utcnow())),
        )

    def recent_feedback(self, limit: int = 20) -> list[sqlite3.Row]:
        """Latest votes with what Claude knew about the offer (for the prompt)."""
        return self.conn.execute(
            "SELECT f.vote, o.title, o.price, o.store, "
            "(SELECT v.name_norm FROM verdicts v WHERE v.offer_id = o.id ORDER BY v.id DESC LIMIT 1) name_norm, "
            "(SELECT v.category FROM verdicts v WHERE v.offer_id = o.id ORDER BY v.id DESC LIMIT 1) category, "
            "(SELECT v.score FROM verdicts v WHERE v.offer_id = o.id ORDER BY v.id DESC LIMIT 1) score "
            "FROM feedback f JOIN offers o ON o.id = f.offer_id ORDER BY f.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def near_misses(self, since: datetime, lo: int, hi: int, limit: int = 5) -> list[sqlite3.Row]:
        """Best offers Claude judged in [lo, hi) since `since`, not fake, not sent."""
        return self.conn.execute(
            "SELECT v.name_norm, MAX(v.score) score, o.price, o.store, o.url, o.title "
            "FROM verdicts v JOIN offers o ON o.id = v.offer_id "
            "WHERE v.created_at >= ? AND v.score >= ? AND v.score < ? AND NOT v.is_fake "
            "AND COALESCE(v.condition, '') NOT IN ('usado', 'recondicionado') "
            "AND abs(o.price - v.price) < 0.01 "
            "AND o.id NOT IN (SELECT offer_id FROM sent) "
            "GROUP BY v.name_norm ORDER BY score DESC, o.price ASC LIMIT ?",
            (_iso(since), lo, hi, limit),
        ).fetchall()

    def sent_count(self, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM sent WHERE sent_at >= ?", (_iso(since),)
        ).fetchone()[0]

    # -- source health ----------------------------------------------------
    def source_ok(self, source: str, now: datetime | None = None) -> bool:
        """Returns True if the source was previously alerted as failing (recovered)."""
        row = self.conn.execute(
            "SELECT alerted FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        self.conn.execute(
            "INSERT INTO source_health (source, consecutive_failures, last_ok, alerted) "
            "VALUES (?, 0, ?, 0) ON CONFLICT(source) DO UPDATE SET "
            "consecutive_failures = 0, last_ok = excluded.last_ok, alerted = 0",
            (source, _iso(now or utcnow())),
        )
        return bool(row and row["alerted"])

    def source_failed(self, source: str, error: str) -> tuple[int, bool]:
        """Returns (consecutive_failures, already_alerted)."""
        self.conn.execute(
            "INSERT INTO source_health (source, consecutive_failures, last_error) "
            "VALUES (?, 1, ?) ON CONFLICT(source) DO UPDATE SET "
            "consecutive_failures = consecutive_failures + 1, last_error = excluded.last_error",
            (source, error[:500]),
        )
        row = self.conn.execute(
            "SELECT consecutive_failures, alerted FROM source_health WHERE source = ?",
            (source,),
        ).fetchone()
        return row["consecutive_failures"], bool(row["alerted"])

    def source_alerted(self, source: str) -> None:
        self.conn.execute("UPDATE source_health SET alerted = 1 WHERE source = ?", (source,))

    # -- verdicts ---------------------------------------------------------
    def get_verdict(self, offer_id: int, price: float) -> Verdict | None:
        row = self.conn.execute(
            "SELECT * FROM verdicts WHERE offer_id = ? AND abs(price - ?) < 0.01",
            (offer_id, price),
        ).fetchone()
        if row is None:
            return None
        return Verdict(
            score=row["score"], fake=bool(row["is_fake"]), flags=json.loads(row["flags_json"]),
            category=row["category"], name_norm=row["name_norm"],
            condition=row["condition"], reason=row["reason"], model=row["model"],
        )

    def save_verdict(self, offer_id: int, price: float, v: Verdict, now: datetime | None = None):
        self.conn.execute(
            "INSERT OR REPLACE INTO verdicts (offer_id, price, score, is_fake, flags_json, "
            "category, name_norm, condition, reason, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (offer_id, price, v.score, int(v.fake), json.dumps(v.flags), v.category,
             v.name_norm, v.condition, v.reason, v.model, _iso(now or utcnow())),
        )
        self.conn.execute("UPDATE offers SET category = ? WHERE id = ?", (v.category, offer_id))

    # -- Claude usage (daily budget) --------------------------------------
    def claude_calls(self, day: date) -> int:
        row = self.conn.execute(
            "SELECT claude_calls FROM usage WHERE day = ?", (day.isoformat(),)
        ).fetchone()
        return row["claude_calls"] if row else 0

    def add_claude_call(self, day: date) -> None:
        self.conn.execute(
            "INSERT INTO usage (day, claude_calls) VALUES (?, 1) ON CONFLICT(day) "
            "DO UPDATE SET claude_calls = claude_calls + 1",
            (day.isoformat(),),
        )

    def add_claude_cost(self, day: date, cost: float) -> None:
        self.conn.execute(
            "UPDATE usage SET cost_usd = cost_usd + ? WHERE day = ?", (cost, day.isoformat())
        )

    def limit_alerted(self, day: date) -> bool:
        row = self.conn.execute(
            "SELECT limit_alerted FROM usage WHERE day = ?", (day.isoformat(),)
        ).fetchone()
        return bool(row and row["limit_alerted"])

    def set_limit_alerted(self, day: date) -> None:
        self.conn.execute(
            "INSERT INTO usage (day, limit_alerted) VALUES (?, 1) ON CONFLICT(day) "
            "DO UPDATE SET limit_alerted = 1",
            (day.isoformat(),),
        )

    # -- dry-run support --------------------------------------------------
    def snapshot(self) -> "Storage":
        """In-memory copy of the whole DB: dry-runs work on it and vanish."""
        copy = Storage.__new__(Storage)
        copy.conn = sqlite3.connect(":memory:", isolation_level=None)
        copy.conn.row_factory = sqlite3.Row
        self.conn.backup(copy.conn)
        return copy

    # -- settings ---------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
