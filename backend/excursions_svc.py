"""超温游标开闭。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import (
    connect,
    now_iso,
    occupancy_guard_decrement,
    occupancy_increment,
    occupancy_open_for_batch,
    write_audit,
)
from locks import release, require_lock, try_acquire
from models import ExcursionCloseIn, ExcursionOpenIn
from validators import detect_excursion

router = APIRouter(tags=["excursions"])


@router.get("/api/excursions")
def list_excursions(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT e.*, b.batch_code FROM excursion_cursors e
               JOIN temp_batches b ON b.id=e.batch_id"""
        if status:
            rows = conn.execute(
                q + " WHERE e.status=? ORDER BY e.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY e.id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/excursions/open")
def open_excursion(body: ExcursionOpenIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"batch:{body.batch_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (body.batch_id,)
            ).fetchone()
            if not batch:
                raise HTTPException(404, "批次不存在")
            if batch["status"] == "已封批":
                raise HTTPException(409, "已封批禁止开游标")
            # 单轨：开放判定只认占用表
            open_n = occupancy_open_for_batch(conn, body.batch_id)
            if open_n:
                raise HTTPException(409, "已有开放游标")
            ch = conn.execute(
                "SELECT target_max FROM cold_chambers WHERE id=?",
                (batch["chamber_id"],),
            ).fetchone()
            threshold = float(body.threshold if body.threshold is not None else ch["target_max"] + 2)
            points = json.loads(batch["temp_points"] or "[]")
            peak = detect_excursion(points, threshold)
            if peak is None:
                raise HTTPException(422, "温度点未超阈，无需开游标")
            key = f"EX-{batch['batch_code']}-{open_n + 1}"
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO excursion_cursors(batch_id,cursor_key,peak_temp,threshold,
                   status,opened_at,note) VALUES (?,?,?,?,?,?,?)""",
                (body.batch_id, key, peak, threshold, "开放", now, body.note),
            )
            # 单轨写口：游标开放与占用计数 +1 同事务，任一失败整体回滚
            occupancy_increment(conn, batch["chamber_id"], body.batch_id)
            conn.execute(
                "UPDATE temp_batches SET status='超温处理中' WHERE id=?",
                (body.batch_id,),
            )
            write_audit(
                conn,
                user,
                "excursion_open",
                key,
                {"batch_id": body.batch_id, "peak": peak},
            )
            conn.commit()
            return {"id": cur.lastrowid, "cursor_key": key, "peak_temp": peak, "status": "开放"}
        finally:
            release(conn, lock_key, user)


@router.post("/api/excursions/close")
def close_excursion(body: ExcursionCloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    # 两名值班同点关同一开放游标：串行占游标锁，后到争用只记痕
    lock_key = f"excursion:{body.cursor_id}"
    conn = connect()
    try:
        if not try_acquire(conn, lock_key, user):
            # 争用留痕须独立落库，不被业务回滚带走
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            row = conn.execute(
                "SELECT * FROM excursion_cursors WHERE id=?", (body.cursor_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "游标不存在")
            if row["status"] != "开放":
                # 后到者在锁串行点之后才进入：游标已被先到者关闭，
                # 不写业务，只补一条争用留痕并独立落库
                conn.execute(
                    """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        lock_key,
                        user,
                        "争用",
                        "后到关闭：游标已闭，只记痕不写业务",
                        now_iso(),
                    ),
                )
                conn.commit()
                raise HTTPException(409, "游标已关闭")
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (row["batch_id"],)
            ).fetchone()

            # 单轨写口（甲路径）：先守卫式零改减一，计数不为负才允许正式关闭；
            # 减一失败即收口计数偏航，关闭整体回滚，只留痕不改游标态。
            if not occupancy_guard_decrement(
                conn, batch["chamber_id"], row["batch_id"]
            ):
                conn.rollback()
                write_audit(
                    conn,
                    user,
                    "excursion_close_count_yaw",
                    row["cursor_key"],
                    {
                        "cursor_id": body.cursor_id,
                        "reason": "开放计数为零或占用行缺失，零改减一将致负，关闭回滚",
                    },
                )
                conn.commit()
                raise HTTPException(409, "收口计数偏航：开放计数不可为负，关闭已回滚")

            now = now_iso()
            cur = conn.execute(
                """UPDATE excursion_cursors SET status='已闭', closed_at=?, note=?
                   WHERE id=? AND status='开放'""",
                (now, body.note or row["note"], body.cursor_id),
            )
            if cur.rowcount != 1:
                # 双重保险：游标在锁内被他路改态，回滚计数扣减
                conn.rollback()
                raise HTTPException(409, "游标状态已变更，关闭已回滚")

            # 该批次再无开放游标：批次态随收口回落，允许后续封批
            if occupancy_open_for_batch(conn, row["batch_id"]) == 0:
                conn.execute(
                    "UPDATE temp_batches SET status='已入库' "
                    "WHERE id=? AND status='超温处理中'",
                    (row["batch_id"],),
                )
            write_audit(
                conn,
                user,
                "excursion_close",
                row["cursor_key"],
                {
                    "cursor_id": body.cursor_id,
                    "open_count_synced": True,
                    "chamber_id": batch["chamber_id"],
                },
            )
            conn.commit()
            return {"id": body.cursor_id, "status": "已闭", "closed_at": now}
        finally:
            release(conn, lock_key, user)
            conn.commit()
    finally:
        conn.close()