from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    workstation TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    conclusion TEXT NOT NULL DEFAULT 'pending'
                        CHECK(conclusion IN ('pending','passed','failed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS measure_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    measure_id INTEGER NOT NULL REFERENCES measures(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    link_state TEXT NOT NULL DEFAULT 'unverified'
                        CHECK(link_state IN ('unverified','verified','recheck')),
                    basis_version INTEGER NOT NULL DEFAULT 1,
                    recheck_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(measure_id, item_id)
                );
                CREATE TABLE IF NOT EXISTS measure_sources (
                    measure_id INTEGER NOT NULL REFERENCES measures(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL,
                    workstation TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(record_id)
                );
                CREATE INDEX IF NOT EXISTS ix_measure_links_item
                    ON measure_links(item_id);
                CREATE TABLE IF NOT EXISTS migration_state (
                    batch TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 共用整改措施台账 ----
    def create_measure(self, code: str, title: str, workstation: str, reason: str,
                       actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO measures(code, title, workstation, reason, conclusion,
                       version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,'pending',1,?,?,?)""",
                    (code, title, workstation, reason, actor, now, now),
                )
                measure_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("措施编号已存在", {"code": code}) from exc
        return self.get_measure(measure_id)

    def get_measure(self, measure_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM measures WHERE id=?", (measure_id,)).fetchone()
        if row is None:
            raise NotFoundError("措施不存在")
        return dict(row)

    def get_measure_by_code(self, code: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM measures WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("措施不存在")
        return dict(row)

    def list_measures(self, conclusion: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM measures"
        params: tuple = ()
        if conclusion:
            sql += " WHERE conclusion=?"
            params = (conclusion,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def measure_links(self, measure_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM measure_links WHERE measure_id=? ORDER BY id",
                (measure_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_item_measures(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.*, m.code, m.title, m.workstation, m.reason,
                          m.conclusion, m.version AS current_version
                   FROM measure_links l JOIN measures m ON m.id=l.measure_id
                   WHERE l.item_id=? ORDER BY l.id""",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def measure_sources(self, measure_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT s.record_id, s.item_id, s.workstation, s.reason,
                          s.batch, s.created_at, r.kind, r.detail, r.status
                   FROM measure_sources s JOIN records r ON r.id=s.record_id
                   WHERE s.measure_id=? ORDER BY s.record_id""",
                (measure_id,)).fetchall()
        return [dict(row) for row in rows]

    def attach_measure(self, measure_id: int, item_id: int, expected_version: int,
                       actor: str) -> Dict[str, Any]:
        """挂接到事故：措施版本CAS先到先得；挂接不改变验收版本，后到者看到当前版本。"""
        now = utc_now()
        with self._lock, self.conn:
            measure = self.conn.execute(
                "SELECT * FROM measures WHERE id=?", (measure_id,)).fetchone()
            if measure is None:
                raise NotFoundError("措施不存在")
            item = self.conn.execute(
                "SELECT id, status FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("事故不存在")
            if measure["conclusion"] == "failed":
                raise ConflictError("措施验收未通过，不能挂接",
                                    {"current_version": measure["version"]})
            cas = self.conn.execute(
                "UPDATE measures SET updated_at=? WHERE id=? AND version=?",
                (now, measure_id, expected_version),
            )
            if cas.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试",
                                    {"current_version": measure["version"]})
            try:
                cur = self.conn.execute(
                    """INSERT INTO measure_links(measure_id, item_id, link_state,
                       basis_version, created_by, created_at, updated_at)
                       VALUES(?,?,'unverified',?,?,?,?)""",
                    (measure_id, item_id, measure["version"], actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该事故已挂接此措施",
                                    {"current_version": measure["version"]}) from exc
            link_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM measure_links WHERE id=?", (link_id,)).fetchone()
        return dict(row)

    def _next_measure_seq(self, conn) -> int:
        rows = conn.execute("SELECT code FROM measures").fetchall()
        seq = 0
        for r in rows:
            code = r["code"]
            if code.startswith("M-") and code[2:].isdigit():
                seq = max(seq, int(code[2:]))
        return seq + 1

    def verify_link(self, link_id: int, actor: str, reason: Optional[str]) -> Dict[str, Any]:
        """事故对挂接措施完成核验：必须与措施当前通过版本一致。"""
        now = utc_now()
        with self._lock, self.conn:
            link = self.conn.execute(
                "SELECT * FROM measure_links WHERE id=?", (link_id,)).fetchone()
            if link is None:
                raise NotFoundError("挂接不存在")
            item = self.conn.execute(
                "SELECT status FROM items WHERE id=?", (link["item_id"],)).fetchone()
            measure = self.conn.execute(
                "SELECT * FROM measures WHERE id=?", (link["measure_id"],)).fetchone()
            if item["status"] != "verification":
                raise ConflictError("事故不在核验状态，不能核验措施")
            if measure["conclusion"] != "passed":
                raise ConflictError("措施尚未验收通过",
                                    {"current_version": measure["version"]})
            if link["basis_version"] != measure["version"]:
                raise ConflictError("措施验收结论已变更，请基于当前版本重新核验",
                                    {"current_version": measure["version"]})
            self.conn.execute(
                """UPDATE measure_links SET link_state='verified', recheck_reason=NULL,
                   updated_at=? WHERE id=?""",
                (now, link_id),
            )
            self.conn.execute(
                """UPDATE records SET status='closed', created_by=created_by
                   WHERE item_id=? AND status='open'
                     AND external_ref GLOB 'RECHECK-'||?||'-*'""",
                (link["item_id"], link["measure_id"]),
            )
            row = self.conn.execute(
                "SELECT * FROM measure_links WHERE id=?", (link_id,)).fetchone()
        return dict(row)

    def recheck_blockers(self, item_id: int) -> List[str]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT m.code, m.conclusion, m.version AS cv, l.basis_version AS bv,
                          l.link_state
                   FROM measure_links l JOIN measures m ON m.id=l.measure_id
                   WHERE l.item_id=?""",
                (item_id,)).fetchall()
        blockers = []
        for r in rows:
            if r["conclusion"] == "failed":
                blockers.append(f"措施{r['code']}验收未通过")
            elif r["link_state"] == "recheck" or r["bv"] != r["cv"]:
                blockers.append(f"措施{r['code']}验收结论已变更，需退回核验")
            elif r["link_state"] != "verified":
                blockers.append(f"措施{r['code']}尚未核验")
        seen = []
        for b in blockers:
            if b not in seen:
                seen.append(b)
        return seen

    def accept_measure(self, measure_id: int, conclusion: str, expected_version: int,
                       actor: str) -> Dict[str, Any]:
        """验收结论变化即失效重算：已核验挂接退回recheck，引用事故退回verification。
        整流程在同一事务+锁内完成，CAS保证先到者生效。"""
        from .rules import MEASURE_ENTITY
        now = utc_now()
        with self._lock, self.conn:
            measure = self.conn.execute(
                "SELECT * FROM measures WHERE id=?", (measure_id,)).fetchone()
            if measure is None:
                raise NotFoundError("措施不存在")
            if measure["conclusion"] == conclusion:
                raise ConflictError("验收结论未发生变化，无需重算",
                                    {"current_version": measure["version"]})
            bumped = self.conn.execute(
                """UPDATE measures SET conclusion=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (conclusion, now, measure_id, expected_version),
            )
            if bumped.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试",
                                    {"current_version": measure["version"]})
            new_version = expected_version + 1
            reason_text = (f"措施{measure['code']}验收结论由"
                           f"{measure['conclusion']}变为{conclusion}，原核验失效，请重新核验")
            links = self.conn.execute(
                "SELECT * FROM measure_links WHERE measure_id=?",
                (measure_id,)).fetchall()
            reopened = []
            for link in links:
                if link["link_state"] == "verified":
                    self.conn.execute(
                        """UPDATE measure_links SET link_state='recheck', basis_version=?,
                           recheck_reason=?, updated_at=? WHERE id=?""",
                        (new_version, reason_text, now, link["id"]),
                    )
                else:
                    self.conn.execute(
                        """UPDATE measure_links SET basis_version=?, recheck_reason=NULL,
                           updated_at=? WHERE id=?""",
                        (new_version, now, link["id"]),
                    )
                    continue
                item = self.conn.execute(
                    "SELECT status, version FROM items WHERE id=?",
                    (link["item_id"],)).fetchone()
                if item["status"] in ("verification", "closed"):
                    ext = f"RECHECK-{measure_id}-{new_version}"
                    exists = self.conn.execute(
                        "SELECT 1 FROM records WHERE item_id=? AND external_ref=?",
                        (link["item_id"], ext)).fetchone()
                    if exists is None:
                        self.conn.execute(
                            """INSERT INTO records(item_id, kind, detail, status,
                               external_ref, created_by, created_at)
                               VALUES(?, 'recheck_reason', ?, 'open', ?, ?, ?)""",
                            (link["item_id"], reason_text, ext, actor, now),
                        )
                    reopened.append({"item_id": link["item_id"],
                                     "from": item["status"]})
                if item["status"] == "closed":
                    self.conn.execute(
                        """UPDATE items SET status='verification', version=version+1,
                           updated_at=? WHERE id=?""",
                        (now, link["item_id"]),
                    )
                    self._append_audit_locked("reopen", "事故", link["item_id"], actor, {
                        "measure_id": measure_id, "measure_code": measure["code"],
                        "from": "closed", "to": "verification", "reason": reason_text,
                    })
            self._append_audit_locked("accept", MEASURE_ENTITY, measure_id, actor, {
                "code": measure["code"], "from": measure["conclusion"],
                "to": conclusion, "version": new_version,
                "invalidated_links": [dict(l) for l in links
                                      if l["link_state"] == "verified"],
                "reopened_items": reopened,
            })
        return self.get_measure(measure_id)

    def _append_audit_locked(self, action, entity_type, entity_id, actor, detail) -> None:
        row = self.conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        self.conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )

    def migrate_legacy_measures(self, actor: str = None, batch: str = None) -> Dict[str, int]:
        """升级时把旧整改明细按工位+原因归并成共用条目；原明细保留，来源和审计可查。可重复执行。"""
        from .rules import (LEGACY_ACTION_KINDS, MEASURE_ENTITY, MIGRATION_ACTOR,
                            MIGRATION_BATCH, legacy_merge_key)
        actor = actor or MIGRATION_ACTOR
        batch = batch or MIGRATION_BATCH
        now = utc_now()
        with self._lock, self.conn:
            done = self.conn.execute(
                "SELECT 1 FROM migration_state WHERE batch=?", (batch,)).fetchone()
            if done is not None:
                return {"measures_created": 0, "records_merged": 0}
            rows = self.conn.execute(
                """SELECT r.* FROM records r
                   WHERE LOWER(r.kind) IN ({})""".format(
                    ",".join("?" * len(LEGACY_ACTION_KINDS))),
                tuple(k.lower() for k in LEGACY_ACTION_KINDS),
            ).fetchall()
            groups: Dict[str, List[sqlite3.Row]] = {}
            identity = {}
            for r in rows:
                key, workstation, reason = legacy_merge_key(dict(r))
                groups.setdefault(key, []).append(r)
                identity[key] = (workstation, reason)
            seq = self._next_measure_seq(self.conn)
            measures_created = 0
            records_merged = 0
            for key, members in groups.items():
                workstation, reason = identity[key]
                code = f"M-{seq}"
                seq += 1
                all_closed = all(m["status"] == "closed" for m in members)
                conclusion = "passed" if all_closed else "pending"
                cur = self.conn.execute(
                    """INSERT INTO measures(code, title, workstation, reason, conclusion,
                       version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,1,?,?,?)""",
                    (code, f"{workstation}：{reason[:60]}", workstation, reason,
                     conclusion, actor, now, now),
                )
                measure_id = int(cur.lastrowid)
                for m in members:
                    state = "verified" if (conclusion == "passed"
                                           and m["status"] == "closed") else "unverified"
                    self.conn.execute(
                        """INSERT INTO measure_links(measure_id, item_id, link_state,
                           basis_version, created_by, created_at, updated_at)
                           VALUES(?,?,?,1,?,?,?)""",
                        (measure_id, m["item_id"], state, actor, now, now),
                    )
                    self.conn.execute(
                        """INSERT INTO measure_sources(measure_id, record_id, item_id,
                           workstation, reason, batch, created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (measure_id, m["id"], m["item_id"], workstation, reason,
                         batch, now),
                    )
                    records_merged += 1
                self._append_audit_locked("migrate", MEASURE_ENTITY, measure_id, actor, {
                    "code": code, "workstation": workstation, "reason": reason,
                    "conclusion": conclusion, "batch": batch,
                    "source_record_ids": [m["id"] for m in members],
                    "source_item_ids": sorted({m["item_id"] for m in members}),
                })
                measures_created += 1
            self.conn.execute(
                "INSERT INTO migration_state(batch, applied_at) VALUES(?,?)",
                (batch, now),
            )
        return {"measures_created": measures_created, "records_merged": records_merged}

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
