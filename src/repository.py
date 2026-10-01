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
        measure_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in __import__('src.rules', fromlist=['MEASURE_STATUSES']).MEASURE_STATUSES)
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
                    workstation TEXT,
                    reason TEXT,
                    measure_id INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS measures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    measure_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    workstation TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    acceptance TEXT NOT NULL DEFAULT 'pending'
                        CHECK(acceptance IN ('pending','accepted','rejected')),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ({measure_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS item_measures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    measure_id INTEGER NOT NULL REFERENCES measures(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL DEFAULT 1,
                    linked_by TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    UNIQUE(item_id, measure_id)
                );
                CREATE INDEX IF NOT EXISTS ix_item_measures_measure
                    ON item_measures(measure_id);
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
            """)
            self._migrate_records_columns()

    def _migrate_records_columns(self) -> None:
        """升级旧库：为records补充工位、原因、措施归属列。"""
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(records)").fetchall()}
        additions = {
            "workstation": "ALTER TABLE records ADD COLUMN workstation TEXT",
            "reason": "ALTER TABLE records ADD COLUMN reason TEXT",
            "measure_id": "ALTER TABLE records ADD COLUMN measure_id INTEGER",
        }
        for name, ddl in additions.items():
            if name not in existing:
                self.conn.execute(ddl)

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
                   external_ref: Optional[str], actor: str,
                   workstation: Optional[str] = None,
                   reason: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       workstation, reason, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref,
                     workstation, reason, actor, now),
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

    # ------------------------------------------------------------------
    # 共用措施台账
    # ------------------------------------------------------------------
    @staticmethod
    def _measure(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _next_measure_no(self, prefix: str) -> str:
        n = 1
        while True:
            candidate = f"{prefix}{n:04d}"
            row = self.conn.execute(
                "SELECT 1 FROM measures WHERE measure_no=?", (candidate,)
            ).fetchone()
            if row is None:
                return candidate
            n += 1

    def create_measure(self, measure_no: str, title: str, workstation: str,
                       reason: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO measures(measure_no, title, workstation, reason,
                       acceptance, status, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,1,?,?,?)""",
                    (measure_no, title, workstation, reason, "pending", "active",
                     actor, now, now),
                )
                measure_id = int(cur.lastrowid)
                self._append_audit("measure_created", "measure", measure_id, actor, {
                    "measure_no": measure_no, "title": title,
                    "workstation": workstation, "reason": reason,
                })
        except sqlite3.IntegrityError as exc:
            raise ConflictError("措施编号已存在") from exc
        return self.get_measure(measure_id)

    def get_measure(self, measure_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM measures WHERE id=?", (measure_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("措施不存在")
        return self._measure(row)

    def get_measure_by_no(self, measure_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM measures WHERE measure_no=?", (measure_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("措施不存在")
        return self._measure(row)

    def list_measures(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM measures"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._measure(row) for row in rows]

    def _measure_version(self, measure_id: int) -> int:
        row = self.conn.execute(
            "SELECT version FROM measures WHERE id=?", (measure_id,)
        ).fetchone()
        return int(row["version"]) if row else 0

    def link_measure(self, measure_id: int, item_id: int,
                     expected_version: int, actor: str) -> Dict[str, Any]:
        """挂接措施到事故。乐观锁：先到者生效，后到者看到当前版本。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM measures WHERE id=?", (measure_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("措施不存在")
            current = self._measure(row)
            if not isinstance(expected_version, int) or expected_version < 1:
                raise ValidationError("expected_version必须是正整数")
            if current["version"] != expected_version:
                raise ConflictError(
                    "版本冲突，请刷新后重试", current_version=current["version"])
            item = self.conn.execute(
                "SELECT id FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            existing = self.conn.execute(
                "SELECT id FROM item_measures WHERE item_id=? AND measure_id=?",
                (item_id, measure_id),
            ).fetchone()
            if existing:
                raise ConflictError("已挂接，无需重复操作")
            self.conn.execute(
                """INSERT INTO item_measures(item_id, measure_id, version,
                   linked_by, linked_at) VALUES(?,?,1,?,?)""",
                (item_id, measure_id, actor, now),
            )
            cur = self.conn.execute(
                "UPDATE measures SET version=version+1, updated_at=? WHERE id=? AND version=?",
                (now, measure_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError(
                    "版本冲突，请刷新后重试",
                    current_version=self._measure_version(measure_id))
            self._append_audit("measure_linked", "measure", measure_id, actor, {
                "item_id": item_id, "item_external_ref": item["external_ref"] if "external_ref" in item.keys() else None,
            })
        return self.get_measure(measure_id)

    def accept_measure(self, measure_id: int, conclusion: str,
                       expected_version: int, reason: str,
                       actor: str) -> Dict[str, Any]:
        """验收措施。结论变化即失效重算，引用事故退回核验并写明原因。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM measures WHERE id=?", (measure_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("措施不存在")
            current = self._measure(row)
            if not isinstance(expected_version, int) or expected_version < 1:
                raise ValidationError("expected_version必须是正整数")
            if current["version"] != expected_version:
                raise ConflictError(
                    "版本冲突，请刷新后重试", current_version=current["version"])
            if conclusion not in ("accepted", "rejected"):
                raise ValidationError("验收结论必须是accepted或rejected")
            if not reason or not str(reason).strip():
                raise ValidationError("验收变化必须写明原因")
            if conclusion == current["acceptance"]:
                # 结论未变：幂等，不失效
                return self.get_measure(measure_id)
            if current["acceptance"] == "pending":
                # 首次验收或失效后重算：建立结论，措施有效
                new_acceptance = conclusion
                new_status = "active"
                pushback = False
            else:
                # 已验收/驳回后结论变化：失效重算，退回核验
                new_acceptance = "pending"
                new_status = "invalid"
                pushback = True
            cur = self.conn.execute(
                """UPDATE measures SET acceptance=?, status=?, version=version+1,
                   updated_at=? WHERE id=? AND version=?""",
                (new_acceptance, new_status, now, measure_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError(
                    "版本冲突，请刷新后重试",
                    current_version=self._measure_version(measure_id))
            if pushback:
                self._rollback_linked_items(measure_id, reason, actor, now)
            else:
                self._append_audit("measure_accepted", "measure", measure_id, actor, {
                    "conclusion": conclusion,
                })
        return self.get_measure(measure_id)

    def _rollback_linked_items(self, measure_id: int, reason: str,
                               actor: str, now: str) -> None:
        """措施失效：引用它的事故退回核验并写明原因。"""
        rows = self.conn.execute(
            "SELECT item_id FROM item_measures WHERE measure_id=?", (measure_id,)
        ).fetchall()
        affected: List[int] = []
        for r in rows:
            item_id = int(r["item_id"])
            item = self.conn.execute(
                "SELECT id, status FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None:
                continue
            if item["status"] in ("corrective_action", "verification", "closed"):
                self.conn.execute(
                    """UPDATE items SET status='verification', version=version+1,
                       updated_at=? WHERE id=?""",
                    (now, item_id),
                )
                affected.append(item_id)
            self.conn.execute(
                """INSERT INTO records(item_id, kind, detail, status, external_ref,
                   workstation, reason, measure_id, created_by, created_at)
                   VALUES(?, 'measure_revoked', ?, 'open', NULL, NULL, NULL, ?, ?, ?)""",
                (item_id, reason, measure_id, actor, now),
            )
        self._append_audit("measure_invalidated", "measure", measure_id, actor, {
            "reason": reason, "affected_items": affected,
        })

    def list_measure_items(self, measure_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT im.item_id, im.linked_by, im.linked_at, im.version AS link_version,
                          i.title, i.status, i.severity, i.version AS item_version,
                          i.external_ref
                   FROM item_measures im JOIN items i ON i.id = im.item_id
                   WHERE im.measure_id=? ORDER BY im.id""",
                (measure_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_item_measures(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT m.* FROM item_measures im JOIN measures m ON m.id = im.measure_id
                   WHERE im.item_id=? ORDER BY m.id""",
                (item_id,),
            ).fetchall()
        return [self._measure(row) for row in rows]

    def list_measure_records(self, measure_id: int) -> List[Dict[str, Any]]:
        """归并到该措施的原明细（旧数据可查）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE measure_id=? ORDER BY id", (measure_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def migrate_to_shared_measures(self, actor: str) -> Dict[str, Any]:
        """升级：把按工位和原因归并的旧措施记录合并成共用条目。原明细与审计可查。"""
        now = utc_now()
        with self._lock, self.conn:
            rows = self.conn.execute(
                """SELECT * FROM records
                   WHERE kind='action' AND measure_id IS NULL
                   ORDER BY id""",
            ).fetchall()
            groups: Dict[tuple, List[sqlite3.Row]] = {}
            for row in rows:
                workstation = row["workstation"] or "未指定工位"
                reason = row["reason"] or "未指定原因"
                key = (workstation, reason)
                groups.setdefault(key, []).append(row)
            created: List[Dict[str, Any]] = []
            for (workstation, reason), recs in groups.items():
                measure_no = self._next_measure_no("CS-MIG-")
                title = f"{workstation}－{reason} 整改措施"
                cur = self.conn.execute(
                    """INSERT INTO measures(measure_no, title, workstation, reason,
                       acceptance, status, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?, 'pending', 'active', 1, ?, ?, ?)""",
                    (measure_no, title, workstation, reason, actor, now, now),
                )
                measure_id = int(cur.lastrowid)
                item_ids = []
                for rec in recs:
                    item_id = int(rec["item_id"])
                    if item_id not in item_ids:
                        item_ids.append(item_id)
                    self.conn.execute(
                        """INSERT OR IGNORE INTO item_measures(item_id, measure_id,
                           version, linked_by, linked_at) VALUES(?,?,1,?,?)""",
                        (item_id, measure_id, actor, now),
                    )
                    self.conn.execute(
                        "UPDATE records SET measure_id=? WHERE id=?",
                        (measure_id, int(rec["id"])),
                    )
                self._append_audit("measure_created", "measure", measure_id, actor, {
                    "measure_no": measure_no, "title": title,
                    "workstation": workstation, "reason": reason,
                    "migrated": True,
                    "source_records": [int(r["id"]) for r in recs],
                    "source_items": item_ids,
                })
                created.append({
                    "measure_id": measure_id, "measure_no": measure_no,
                    "workstation": workstation, "reason": reason,
                    "record_count": len(recs), "item_ids": item_ids,
                })
            self._append_audit("migrate_measures", "measure", 0, actor, {
                "created": len(created),
                "measure_ids": [c["measure_id"] for c in created],
            })
        return {"created": created, "created_count": len(created)}

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._append_audit(action, entity_type, entity_id, actor, detail)

    def _append_audit(self, action: str, entity_type: str, entity_id: int,
                      actor: str, detail: dict) -> Dict[str, Any]:
        """必须已处于 self.conn 事务中；供批量操作在同一事务内写审计链。"""
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

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        clauses = []
        if entity_id is not None:
            clauses.append("entity_id=?")
            params += (entity_id,)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params += (entity_type,)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
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
