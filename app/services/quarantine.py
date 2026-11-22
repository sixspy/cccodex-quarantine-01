from __future__ import annotations

import hashlib
import sqlite3
import threading

from app.db import get_db
from app.services import audit as audit_svc
from app.services import settings as settings_svc
from app.services.errors import ServiceError

# 入舱落点口径：旁路凭证表（唯一选边，不与物化文件并开）。
# 入舱互斥锁：同进程内串行化入舱写事务，配合 BEGIN IMMEDIATE 防跨进程双写覆盖。
_ENTRY_LOCK = threading.RLock()

# 只读口径项（常量定义在 settings，避免循环导入），此处再转出供 seed 等引用
LANDING_MODE_KEY = settings_svc.LANDING_MODE_KEY
LANDING_MODE_VALUE = settings_svc.LANDING_MODE_VALUE

_FP_SEP = "\x1f"


def fingerprint_of(item_key: str, quality: str, payload: str) -> str:
    """样本指纹：条目键 + 质量标记 + 载荷，入舱瞬间与后续复核各算一遍比对。"""
    h = hashlib.sha256()
    h.update(str(item_key).encode("utf-8"))
    h.update(_FP_SEP.encode("utf-8"))
    h.update(str(quality).encode("utf-8"))
    h.update(_FP_SEP.encode("utf-8"))
    h.update(str(payload).encode("utf-8"))
    return h.hexdigest()


def _fingerprint_status(conn: sqlite3.Connection, row_id: int) -> dict:
    v = conn.execute(
        "SELECT * FROM entry_vouchers WHERE row_id=?", (row_id,)
    ).fetchone()
    if not v:
        return {
            "fingerprint_ok": False,
            "missing_voucher": True,
            "voucher_fingerprint": "",
            "current_fingerprint": "",
        }
    item = conn.execute(
        "SELECT item_key, quality, payload FROM capture_items WHERE id=?",
        (v["item_id"],),
    ).fetchone()
    current = (
        fingerprint_of(item["item_key"], item["quality"], item["payload"])
        if item
        else ""
    )
    return {
        "fingerprint_ok": current == v["sample_fingerprint"],
        "missing_voucher": False,
        "voucher_fingerprint": v["sample_fingerprint"],
        "current_fingerprint": current,
    }


def assert_fingerprint_intact(conn: sqlite3.Connection, row_id: int) -> None:
    """复核继续 / 清洗入库前的统一闸门：指纹拧花或凭证缺失一律拦下。"""
    st = _fingerprint_status(conn, row_id)
    if st["missing_voucher"]:
        raise ServiceError("入舱凭证缺失，禁止复核与清洗入库")
    if not st["fingerprint_ok"]:
        raise ServiceError("样本指纹与舱内凭证拧花，禁止复核与清洗入库")


def list_bins(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        landing_mode = settings_svc.get_value(
            settings_svc.LANDING_MODE_KEY,
            settings_svc.LANDING_MODE_VALUE,
            db_path,
        )
        rows = conn.execute(
            """
            SELECT b.*,
              (SELECT COUNT(*) FROM quarantine_rows r
                 WHERE r.bin_id=b.id AND r.status='待复核') AS pending_count,
              (SELECT COUNT(*) FROM quarantine_rows r
                 WHERE r.bin_id=b.id) AS row_count,
              (SELECT group_concat(DISTINCT v.source_name)
                 FROM quarantine_rows rr
                 JOIN entry_vouchers v ON v.row_id=rr.id
                WHERE rr.bin_id=b.id) AS source_names,
              (SELECT group_concat(DISTINCT v.quality_snapshot)
                 FROM quarantine_rows rr
                 JOIN entry_vouchers v ON v.row_id=rr.id
                WHERE rr.bin_id=b.id) AS quality_names
            FROM quarantine_bins b ORDER BY b.id
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = [s for s in (d.pop("source_names") or "").split(",") if s]
            d["qualities"] = [s for s in (d.pop("quality_names") or "").split(",") if s]
            # 入舱落点徽：卡片上亮明当前读集合走的是哪一轨（只此一轨，不并存）
            d["landing_mode"] = landing_mode
            out.append(d)
        return out


def list_rows(bin_id: int | None = None, db_path: str | None = None) -> list[dict]:
    sql = """
        SELECT r.id, r.bin_id, r.item_id, r.reason, r.status, r.entered_at,
               b.bin_code, b.title AS bin_title,
               v.item_key, v.payload_snapshot, v.quality_snapshot,
               v.source_name, v.sample_fingerprint,
               i.item_key AS live_item_key, i.quality AS live_quality,
               i.payload AS live_payload
        FROM quarantine_rows r
        JOIN quarantine_bins b ON b.id=r.bin_id
        LEFT JOIN entry_vouchers v ON v.row_id=r.id
        LEFT JOIN capture_items i ON i.id=r.item_id
        {where}
        ORDER BY r.id DESC
    """
    with get_db(db_path) as conn:
        if bin_id:
            rows = conn.execute(sql.format(where="WHERE r.bin_id=?"), (bin_id,)).fetchall()
        else:
            rows = conn.execute(sql.format(where="")).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            live = fingerprint_of(
                d["live_item_key"] or "", d["live_quality"] or "", d["live_payload"] or ""
            )
            d["fingerprint_ok"] = (
                bool(d["sample_fingerprint"]) and live == d["sample_fingerprint"]
            )
            for k in ("live_item_key", "live_quality", "live_payload"):
                d.pop(k, None)
            out.append(d)
        return out


def enter_item(item_id: int, bin_id: int, reason: str, db_path: str | None = None) -> dict:
    reason = (reason or "").strip() or "质量异常入舱"
    with _ENTRY_LOCK:
        with get_db(db_path) as conn:
            try:
                # 整笔落完：先拿写锁，杜绝两人争抢同一件时双份凭证互相覆盖
                conn.execute("BEGIN IMMEDIATE")
                item = conn.execute(
                    "SELECT * FROM capture_items WHERE id=?", (item_id,)
                ).fetchone()
                if not item:
                    raise ServiceError("条目不存在", 404)
                if item["status"] != "待检":
                    raise ServiceError("仅待检条目可入舱")
                if item["quality"] == "合格":
                    raise ServiceError("合格条目不应入舱")
                bin_row = conn.execute(
                    "SELECT * FROM quarantine_bins WHERE id=?", (bin_id,)
                ).fetchone()
                if not bin_row:
                    raise ServiceError("隔离舱不存在", 404)
                if bin_row["status"] != "开放":
                    raise ServiceError("隔离舱未开放")
                open_bins = conn.execute(
                    "SELECT COUNT(*) AS c FROM quarantine_bins WHERE status='开放'"
                ).fetchone()["c"]
                # 同一连接内读参数，避免写事务已持有 RESERVED 锁时再开连接撞锁
                max_open_row = conn.execute(
                    "SELECT value FROM lab_settings WHERE key='max_bin_open'"
                ).fetchone()
                max_open = int((max_open_row["value"] if max_open_row else "3") or "3")
                if open_bins > max_open:
                    raise ServiceError("开放隔离舱超过上限")
                occupied = conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM quarantine_rows
                    WHERE bin_id=? AND status IN ('待复核','回炉中')
                    """,
                    (bin_id,),
                ).fetchone()["c"]
                if occupied >= bin_row["capacity"]:
                    raise ServiceError("隔离舱已满")
                exists = conn.execute(
                    "SELECT id FROM quarantine_rows WHERE item_id=?", (item_id,)
                ).fetchone()
                if exists:
                    raise ServiceError("条目已在隔离舱")
                batch = conn.execute(
                    "SELECT source_name FROM capture_batches WHERE id=?",
                    (item["batch_id"],),
                ).fetchone()
                source_name = batch["source_name"] if batch else ""
                cur = conn.execute(
                    """
                    INSERT INTO quarantine_rows(bin_id, item_id, reason, status)
                    VALUES (?, ?, ?, '待复核')
                    """,
                    (bin_id, item_id, reason),
                )
                row_id = cur.lastrowid
                # 旁路凭证：锁写入舱瞬间的条目键、载荷、质量标记与指纹，只 INSERT 不 UPDATE
                fp = fingerprint_of(
                    item["item_key"], item["quality"], item["payload"]
                )
                conn.execute(
                    """
                    INSERT INTO entry_vouchers(
                      row_id, item_id, item_key, payload_snapshot, quality_snapshot,
                      batch_id, source_name, sample_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        item_id,
                        item["item_key"],
                        item["payload"],
                        item["quality"],
                        item["batch_id"],
                        source_name,
                        fp,
                    ),
                )
                conn.execute(
                    "UPDATE capture_items SET status='入舱' WHERE id=?", (item_id,)
                )
                n = conn.execute(
                    "SELECT COUNT(*) AS c FROM review_tickets"
                ).fetchone()["c"] + 1
                conn.execute(
                    """
                    INSERT INTO review_tickets(ticket_code, row_id, decision, comment)
                    VALUES (?, ?, '待决', ?)
                    """,
                    (f"REV-{n:04d}", row_id, "入舱自动开复核单"),
                )
                row = conn.execute(
                    """
                    SELECT r.*, v.item_key, v.quality_snapshot AS quality,
                           v.payload_snapshot AS payload, v.source_name,
                           v.sample_fingerprint, b.bin_code
                    FROM quarantine_rows r
                    JOIN entry_vouchers v ON v.row_id=r.id
                    JOIN quarantine_bins b ON b.id=r.bin_id
                    WHERE r.id=?
                    """,
                    (row_id,),
                ).fetchone()
                data = dict(row)
            except sqlite3.IntegrityError:
                # UNIQUE(item_id) / UNIQUE(row_id) 兜底：并发下后来者拿中文短句
                raise ServiceError("该坏件已入舱，请勿重复入舱")
            except sqlite3.OperationalError as exc:
                # 跨进程争抢写锁超时：整笔不落，回中文原因
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise ServiceError("入舱写锁争用，请稍后重试")
                raise
    audit_svc.write_event("入舱", "quarantine_row", row_id, reason, db_path=db_path)
    return data


def auto_enter_bad(batch_id: int, bin_id: int, db_path: str | None = None) -> dict:
    auto = settings_svc.get_value("auto_enter_bad", "1", db_path)
    if auto != "1":
        raise ServiceError("未开启自动入舱")
    with get_db(db_path) as conn:
        items = conn.execute(
            """
            SELECT id FROM capture_items
            WHERE batch_id=? AND quality!='合格' AND status='待检'
            """,
            (batch_id,),
        ).fetchall()
    entered = []
    for it in items:
        entered.append(enter_item(it["id"], bin_id, "批内自动入舱", db_path))
    return {"entered": len(entered), "rows": entered}


def backfill_vouchers_conn(conn: sqlite3.Connection) -> int:
    """给历史舱行补旁路凭证（幂等），保证一舱行一凭证。"""
    rows = conn.execute(
        """
        SELECT r.id AS row_id, r.item_id, i.item_key, i.payload, i.quality,
               i.batch_id, cb.source_name
        FROM quarantine_rows r
        JOIN capture_items i ON i.id=r.item_id
        JOIN capture_batches cb ON cb.id=i.batch_id
        WHERE NOT EXISTS (SELECT 1 FROM entry_vouchers v WHERE v.row_id=r.id)
        """
    ).fetchall()
    for r in rows:
        fp = fingerprint_of(r["item_key"], r["quality"], r["payload"])
        conn.execute(
            """
            INSERT INTO entry_vouchers(
              row_id, item_id, item_key, payload_snapshot, quality_snapshot,
              batch_id, source_name, sample_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["row_id"],
                r["item_id"],
                r["item_key"],
                r["payload"],
                r["quality"],
                r["batch_id"],
                r["source_name"],
                fp,
            ),
        )
    return len(rows)


def backfill_vouchers(db_path: str | None = None) -> int:
    with get_db(db_path) as conn:
        return backfill_vouchers_conn(conn)
