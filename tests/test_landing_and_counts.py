from __future__ import annotations

import sqlite3

from app.db import get_db
from app.seed import seed_if_needed
from app.services import captures, quarantine, review, settings
from app.services.errors import ServiceError


def _bad_item(db_path):
    for batch in captures.list_batches(db_path):
        if batch["status"] == "已关闭":
            continue
        detail = captures.get_batch(batch["id"], db_path)
        for item in detail["items"]:
            if item["quality"] != "合格" and item["status"] == "待检":
                return batch, item
    raise AssertionError("测试数据里应有待检坏件")


def _open_bin(db_path):
    return next(b for b in quarantine.list_bins(db_path) if b["status"] == "开放")


def test_bad_count_drops_after_entering(db_path):
    batch, item = _bad_item(db_path)
    before = next(b for b in captures.list_batches(db_path) if b["id"] == batch["id"])
    assert before["bad_count"] >= 1

    quarantine.enter_item(item["id"], _open_bin(db_path)["id"], "计数回落测试", db_path)

    after = next(b for b in captures.list_batches(db_path) if b["id"] == batch["id"])
    assert after["bad_count"] == before["bad_count"] - 1
    # 批次详情与列表同口径
    detail = captures.get_batch(batch["id"], db_path)
    assert detail["bad_count"] == after["bad_count"]
    # 条目总数不因入舱而减少，只是坏件不再计在抓取侧
    assert after["item_count"] == before["item_count"]

    # 该坏件此时只出现在隔离舱一侧，不再漂在抓取坏件计数里
    landed = [r for r in quarantine.list_rows(None, db_path) if r["item_id"] == item["id"]]
    assert len(landed) == 1


def test_bin_card_shows_landing_mode_badge(db_path):
    bins = quarantine.list_bins(db_path)
    assert bins
    assert all(b["landing_mode"] == settings.LANDING_MODE_VALUE for b in bins)
    # 落点口径只此一条，设置页可见且只读
    landing = next(s for s in settings.list_settings(db_path) if s["key"] == settings.LANDING_MODE_KEY)
    assert landing["readonly"] == 1


def test_review_detail_carries_audit_trail(db_path):
    batch, item = _bad_item(db_path)
    row = quarantine.enter_item(item["id"], _open_bin(db_path)["id"], "详情旁挂审计", db_path)
    ticket = next(t for t in review.list_tickets(db_path) if t["row_id"] == row["id"])
    assert ticket["audit"]
    assert any(a["action"] == "入舱" for a in ticket["audit"])


def test_old_db_upgrade_exposes_readonly_landing_mode(tmp_path, monkeypatch):
    """老库（已 seeded、无凭证表与口径项）升级后也要露出只读切流口径。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE capture_batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_code TEXT UNIQUE, source_name TEXT,
          status TEXT, item_count INT DEFAULT 0, bad_count INT DEFAULT 0,
          note TEXT DEFAULT '', created_at TEXT DEFAULT '');
        CREATE TABLE capture_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INT, item_key TEXT,
          payload TEXT DEFAULT '', quality TEXT, status TEXT, created_at TEXT DEFAULT '');
        CREATE TABLE quarantine_bins (
          id INTEGER PRIMARY KEY AUTOINCREMENT, bin_code TEXT UNIQUE, title TEXT,
          status TEXT, capacity INT, note TEXT DEFAULT '', created_at TEXT DEFAULT '');
        CREATE TABLE quarantine_rows (
          id INTEGER PRIMARY KEY AUTOINCREMENT, bin_id INT, item_id INT UNIQUE,
          reason TEXT, status TEXT, entered_at TEXT DEFAULT '');
        CREATE TABLE lab_settings (key TEXT PRIMARY KEY, value TEXT, label TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('seeded','1');
        INSERT INTO capture_batches(batch_code, source_name, status)
          VALUES('CAP-LEGACY','老源','已收齐');
        INSERT INTO capture_items(batch_id, item_key, payload, quality, status)
          VALUES(1,'sku-legacy','{"t":""}','缺字段','入舱');
        INSERT INTO quarantine_bins(bin_code, title, status, capacity)
          VALUES('BIN-A','老舱','开放',10);
        INSERT INTO quarantine_rows(bin_id, item_id, reason, status)
          VALUES(1,1,'历史行','待复核');
        """
    )
    conn.commit()
    conn.close()

    from app import config

    monkeypatch.setenv("DATABASE_PATH", str(path))
    config.Config.DATABASE_PATH = path
    config.Config.INSTANCE_DIR = tmp_path
    seed_if_needed(str(path))

    with get_db(str(path)) as c:
        landed = c.execute(
            "SELECT value FROM lab_settings WHERE key=?", (settings.LANDING_MODE_KEY,)
        ).fetchone()
        voucher = c.execute("SELECT * FROM entry_vouchers WHERE row_id=1").fetchone()
    assert landed is not None
    assert landed["value"] == settings.LANDING_MODE_VALUE
    assert voucher is not None
    # 口径项仍不可改
    try:
        settings.update_setting(settings.LANDING_MODE_KEY, "物化文件", str(path))
    except ServiceError as exc:
        assert "只读" in exc.message
    else:
        raise AssertionError("只读口径不应被改")
