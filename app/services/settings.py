from __future__ import annotations

from app.db import get_db
from app.services.errors import ServiceError

# 入舱落点口径：只兑现一边（旁路凭证表），未启用物化文件方案。
# 只许在台参数页看、不许改的口径项
LANDING_MODE_KEY = "entry_landing_mode"
LANDING_MODE_VALUE = "旁路凭证表"
_READONLY_KEYS = frozenset({LANDING_MODE_KEY})


def list_settings(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value, label FROM lab_settings ORDER BY key"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["readonly"] = 1 if d["key"] in _READONLY_KEYS else 0
            out.append(d)
        return out


def update_setting(key: str, value: str, db_path: str | None = None) -> dict:
    if not key or value is None:
        raise ServiceError("参数不完整")
    if key in _READONLY_KEYS:
        raise ServiceError("入舱落点口径为只读项，不能修改")
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT key, value, label FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        if not row:
            raise ServiceError("设置项不存在", 404)
        conn.execute("UPDATE lab_settings SET value=? WHERE key=?", (str(value), key))
        out = conn.execute(
            "SELECT key, value, label FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        data = dict(out)
        data["readonly"] = 0
        return data


def get_value(key: str, default: str = "", db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM lab_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
