import sqlite3
import threading

import pytest

from app.db import get_db
from app.services import captures, quarantine, reconcile, review, rework, settings
from app.services.errors import ServiceError


def _one_pending_bad(db_path):
    batches = captures.list_batches(db_path)
    batch = next(b for b in batches if b["status"] != "已关闭")
    detail = captures.get_batch(batch["id"], db_path)
    bad = next(i for i in detail["items"] if i["quality"] != "合格" and i["status"] == "待检")
    bad["batch_id"] = batch["id"]
    bins = quarantine.list_bins(db_path)
    open_bin = next(b for b in bins if b["status"] == "开放")
    return bad, open_bin


def test_voucher_locks_entry_moment_and_blocks_drift(db_path):
    bad, open_bin = _one_pending_bad(db_path)
    row = quarantine.enter_item(bad["id"], open_bin["id"], "入舱快照测试", db_path)
    original_payload = bad["payload"]
    original_quality = bad["quality"]

    # 入舱后源条目被刷新（漂件）：凭证快照仍是入舱那一刻
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE capture_items SET payload=?, quality=? WHERE id=?",
            ('{"title":"被改写的新载荷"}', "合格", bad["id"]),
        )

    rows = quarantine.list_rows(open_bin["id"], db_path)
    mine = next(rr for rr in rows if rr["id"] == row["id"])
    assert mine["item_key"] == bad["item_key"]
    assert mine["payload_snapshot"] == original_payload
    assert mine["quality_snapshot"] == original_quality
    assert mine["fingerprint_ok"] is False

    # 复核详情同样读凭证快照，与入舱那一刻对得上
    tickets = review.list_tickets(db_path)
    ticket = next(t for t in tickets if t["row_id"] == row["id"])
    assert ticket["payload"] == original_payload
    assert ticket["quality"] == original_quality
    assert ticket["fingerprint_ok"] is False

    # 指纹拧花：复核继续被拦，中文短句
    with pytest.raises(ServiceError) as ei:
        review.decide(ticket["id"], "放行", "admin", "试放行", db_path)
    assert "指纹" in ei.value.message

    # 清洗入库（回炉执行）同样被拦，不误吃漂件
    with pytest.raises(ServiceError):
        rework.enqueue_from_row(row["id"], db_path)


def test_rework_blocked_when_fingerprint_drifts_after_enqueue(db_path):
    bad, open_bin = _one_pending_bad(db_path)
    row = quarantine.enter_item(bad["id"], open_bin["id"], "先入队后漂移", db_path)
    rework.enqueue_from_row(row["id"], db_path)  # 指纹完好时先入队
    jobs = rework.list_jobs(db_path)
    job = next(jj for jj in jobs if jj["row_id"] == row["id"])

    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE capture_items SET payload=? WHERE id=?",
            (" drifted payload ", bad["id"]),
        )

    with pytest.raises(ServiceError) as ei:
        rework.run_job(job["id"], succeed=True, db_path=db_path)
    assert "指纹" in ei.value.message


def test_missing_voucher_blocks_review(db_path):
    bad, open_bin = _one_pending_bad(db_path)
    row = quarantine.enter_item(bad["id"], open_bin["id"], "凭证缺失测试", db_path)
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM entry_vouchers WHERE row_id=?", (row["id"],))
    tickets = review.list_tickets(db_path)
    ticket = next((tt for tt in tickets if tt["row_id"] == row["id"]), None)
    # 凭证行缺失时复核单不参与正常复核列表
    assert ticket is None
    # 直接裁决也被闸门拦下
    with get_db(db_path) as conn:
        t = conn.execute(
            "SELECT id FROM review_tickets WHERE row_id=?", (row["id"],)
        ).fetchone()
    with pytest.raises(ServiceError) as ei:
        review.decide(t["id"], "放行", db_path=db_path)
    assert "凭证" in ei.value.message


def test_concurrent_enter_same_item_only_one_voucher(db_path):
    bad, open_bin = _one_pending_bad(db_path)
    errors = []

    def worker():
        try:
            quarantine.enter_item(bad["id"], open_bin["id"], "争抢入舱", db_path)
        except ServiceError as exc:
            errors.append(exc.message)
        except sqlite3.Error as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 恰好一边兑现，另一边拿中文短句
    assert len(errors) == 1
    with get_db(db_path) as conn:
        row_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_rows WHERE item_id=?", (bad["id"],)
        ).fetchone()["c"]
        voucher_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM entry_vouchers WHERE item_id=?", (bad["id"],)
        ).fetchone()["c"]
        item_status = conn.execute(
            "SELECT status FROM capture_items WHERE id=?", (bad["id"],)
        ).fetchone()["status"]
    assert row_cnt == 1
    assert voucher_cnt == 1
    assert item_status == "入舱"


def test_landing_setting_is_readonly(db_path):
    items = settings.list_settings(db_path)
    landing = next(s for s in items if s["key"] == settings.LANDING_MODE_KEY)
    assert landing["value"] == "旁路凭证表"
    assert landing["readonly"] == 1
    with pytest.raises(ServiceError) as ei:
        settings.update_setting(settings.LANDING_MODE_KEY, "物化文件", db_path)
    assert "只读" in ei.value.message


def test_backfill_vouchers_for_legacy_rows(db_path):
    bad, open_bin = _one_pending_bad(db_path)
    # 手工造一条无凭证的历史舱行
    with get_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO quarantine_rows(bin_id, item_id, reason, status) VALUES (?,?,?,?)",
            (open_bin["id"], bad["id"], "历史漏凭证", "待复核"),
        )
        legacy_row_id = cur.lastrowid
    n = quarantine.backfill_vouchers(db_path)
    assert n >= 1
    with get_db(db_path) as conn:
        v = conn.execute(
            "SELECT * FROM entry_vouchers WHERE row_id=?", (legacy_row_id,)
        ).fetchone()
    assert v is not None
    assert v["item_key"] == bad["item_key"]
    assert v["quality_snapshot"] == bad["quality"]
    # 幂等：再跑不重复补
    assert quarantine.backfill_vouchers(db_path) == 0


def test_bin_card_carries_source_chips(db_path):
    bad, open_bin = _one_pending_bad(db_path)
    quarantine.enter_item(bad["id"], open_bin["id"], "来源芯片测试", db_path)
    bins = quarantine.list_bins(db_path)
    target = next(bb for bb in bins if bb["id"] == open_bin["id"])
    detail = captures.get_batch(bad["batch_id"], db_path)
    batch_source = next(
        bb["source_name"]
        for bb in captures.list_batches(db_path)
        if bb["id"] == bad["batch_id"]
    )
    assert batch_source in target["sources"]
    assert bad["quality"] in target["qualities"]
    assert detail  # 批次详情仍可读


def test_reconcile_flags_missing_voucher(db_path):
    bad, open_bin = _one_pending_bad(db_path)
    row = quarantine.enter_item(bad["id"], open_bin["id"], "对账缺凭证", db_path)
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM entry_vouchers WHERE row_id=?", (row["id"],))
    report = reconcile.run_reconcile(db_path)
    assert "缺凭证1" in report["summary"]
    assert report["mismatch"] >= 1
