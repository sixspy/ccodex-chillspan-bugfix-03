"""BUGFIX-03 超温游标收口与仓间开放游标计数单轨一致性。

顺序铁律（禁止跳过）：
  红甲：关游标成功而仓间开放计数未降 —— 盯游标已关、计数未减。
  红乙：扣减非负护栏 —— 盯影子式半收口（计数已降而游标未正式关闭）被拒写，
        且计数永不为负；偏离须记“收口计数偏航”并回滚。
  红争用：两名值班同点关同一开放游标 —— 串行占游标锁，后到争用只记痕，
        禁止双人双成功。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _open_seed_cursor(c: TestClient, h: dict, code: str = "TB-FIX03-001"):
    """建一个超温批次并打开游标，返回 (batch, cursor, chamber_id)。"""
    chambers = c.get("/api/chambers", headers=h).json()["items"]
    ch = next(x for x in chambers if x["chamber_code"] == "CK-HN-03")
    probes = c.get(f"/api/probes?chamber_id={ch['id']}", headers=h).json()["items"]
    probe = probes[0]
    points = [
        {"t": "10:00", "c": 3.0},
        {"t": "11:00", "c": 9.5},
        {"t": "12:00", "c": 4.0},
    ]
    batch = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": "收口计数夹具货载",
            "temp_points": points,
            "batch_code": code,
        },
    ).json()
    exc = c.post(
        "/api/excursions/open",
        headers=h,
        json={"batch_id": batch["id"], "threshold": 7.0},
    )
    assert exc.status_code == 200, exc.text
    return batch, exc.json(), ch["id"]


def _chamber_open_count(chamber_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) c FROM excursion_cursors e
               JOIN temp_batches b ON b.id=e.batch_id
               WHERE b.chamber_id=? AND e.status='开放'""",
            (chamber_id,),
        ).fetchone()["c"]


def _occupancy_open_count(chamber_id: int) -> int:
    """占用表（单轨正式计数）读数；表不存在时返回 -1 以暴露未建表。"""
    with connect() as conn:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chamber_excursion_occupancy'"
        ).fetchone()
        if not has:
            return -1
        row = conn.execute(
            """SELECT COALESCE(SUM(open_count),0) c
               FROM chamber_excursion_occupancy WHERE chamber_id=?""",
            (chamber_id,),
        ).fetchone()
        return int(row["c"])


# ---------------- 红甲：关游标成功，开放计数必须同事务递减 ----------------

def test_red_a_close_must_decrement_open_count_same_tx():
    c = TestClient(app)
    h = _auth(c)
    batch, cursor, chamber_id = _open_seed_cursor(c, h)

    before_view = _chamber_open_count(chamber_id)
    assert before_view >= 1
    occ_before = _occupancy_open_count(chamber_id)
    assert occ_before == before_view  # 占用表与游标视图开放数对齐

    r = c.post(
        "/api/excursions/close",
        headers=h,
        json={"cursor_id": cursor["id"], "note": "甲路径正式关闭"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "已闭"

    # 游标视图：该游标确已正式关闭
    listed = c.get("/api/excursions", headers=h).json()["items"]
    mine = next(x for x in listed if x["id"] == cursor["id"])
    assert mine["status"] == "已闭" and mine["closed_at"]

    # 仓间视图开放计数必须同步递减，不得仍把已闭游标计入
    detail = c.get(f"/api/chambers/{chamber_id}", headers=h).json()
    assert detail["open_excursion_count"] == before_view - 1

    # 单轨占用表同事务扣减，与游标视图开放数一致
    assert _occupancy_open_count(chamber_id) == before_view - 1
    assert _chamber_open_count(chamber_id) == before_view - 1

    # 监控汇总（仪表盘）也不得再显示该区间未收口
    dash = c.get("/api/dashboard", headers=h).json()
    assert dash["open_excursions"] == _chamber_open_count_all(c, h)

    # 批次态随最后一个游标收口回落，允许封批
    got = c.get(f"/api/batches/{batch['id']}", headers=h).json()["batch"]
    assert got["status"] == "已入库"


def _chamber_open_count_all(c: TestClient, h: dict) -> int:
    rows = c.get("/api/excursions?status=开放", headers=h).json()["items"]
    return len(rows)


# ---------------- 红乙：禁止半收口，计数非负护栏与偏航拒写 ----------------

def test_red_b_no_half_close_and_negative_guard_rejects():
    c = TestClient(app)
    h = _auth(c)
    batch, cursor, chamber_id = _open_seed_cursor(c, h, code="TB-FIX03-002")
    before = _chamber_open_count(chamber_id)

    # 人为制造“乙路径式半收口”现场：正式占用计数被先扣掉，
    # 但游标草案并未晋升为正式关闭（仍为开放）。
    with connect() as conn:
        conn.execute(
            "UPDATE chamber_excursion_occupancy SET open_count=0 "
            "WHERE chamber_id=? AND batch_id=?",
            (chamber_id, batch["id"]),
        ) if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='chamber_excursion_occupancy'"
        ).fetchone() else None
        conn.commit()

    r = c.post(
        "/api/excursions/close",
        headers=h,
        json={"cursor_id": cursor["id"], "note": "护栏触发关闭"},
    )
    # 计数已为零：零改减一会变负，必须拒写并回滚，不得“游标已关而计数虚高/失真”
    assert r.status_code == 409, r.text

    # 游标不得被半收口：仍为开放
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM excursion_cursors WHERE id=?", (cursor["id"],)
        ).fetchone()
        assert row["status"] == "开放"
        neg = conn.execute(
            "SELECT COUNT(*) c FROM chamber_excursion_occupancy WHERE open_count<0"
        ).fetchone()["c"] if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='chamber_excursion_occupancy'"
        ).fetchone() else 0
        assert neg == 0  # 计数永不为负

    # 偏离须留痕：收口计数偏航
    audits = c.get("/api/audit?limit=500", headers=h).json()["items"]
    assert any(
        a["action"] == "excursion_close_count_yaw"
        and str(cursor["id"]) in str(a.get("payload", ""))
        for a in audits
    ), "偏离未记收口计数偏航"

    # 视图读数仍与占用表一致（半收口未被承认）
    detail = c.get(f"/api/chambers/{chamber_id}", headers=h).json()
    assert detail["open_excursion_count"] == before - 1
    assert _occupancy_open_count(chamber_id) == before - 1


# ---------------- 红争用：双人同游标抢关，禁止双成功 ----------------

def test_red_contention_concurrent_close_serialized():
    c = TestClient(app)
    h1 = _auth(c, "admin")
    h2 = _auth(c, "ops")
    batch, cursor, chamber_id = _open_seed_cursor(c, h1, code="TB-FIX03-003")
    before = _chamber_open_count(chamber_id)

    results: list = []
    barrier = threading.Barrier(2)

    def close_cursor(headers: dict):
        local = TestClient(app)
        barrier.wait()
        rr = local.post(
            "/api/excursions/close",
            headers=headers,
            json={"cursor_id": cursor["id"], "note": "双人抢关"},
        )
        results.append((rr.status_code, rr.json() if rr.headers.get("content-type", "").startswith("application/json") else {}))

    t1 = threading.Thread(target=close_cursor, args=(h1,))
    t2 = threading.Thread(target=close_cursor, args=(h2,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    statuses = sorted(s for s, _ in results)
    # 恰好一个成功，另一个争用/已闭拒绝 —— 禁止双成功
    assert statuses == [200, 409], results

    # 开放计数恰好只减一次
    assert _chamber_open_count(chamber_id) == before - 1
    assert _occupancy_open_count(chamber_id) == before - 1
    with connect() as conn:
        closed_n = conn.execute(
            "SELECT COUNT(*) c FROM excursion_cursors WHERE id=? AND status='已闭'",
            (cursor["id"],),
        ).fetchone()["c"]
        assert closed_n == 1
        close_audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='excursion_close'"
        ).fetchone()["c"]
        assert close_audits == 1
        traces = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?",
            (f"excursion:{cursor['id']}",),
        ).fetchone()["c"]
        assert traces >= 1  # 后到争用只记痕


# ---------------- 绿：五视图读数对齐与贯通链路 ----------------

def test_green_five_views_aligned_after_close():
    c = TestClient(app)
    h = _auth(c)
    batch, cursor, chamber_id = _open_seed_cursor(c, h, code="TB-FIX03-004")

    # 关前：五个前台读数面对同一开放游标
    chamber_code = next(
        x["chamber_code"] for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["id"] == chamber_id
    )
    open_list = c.get("/api/excursions?status=开放", headers=h).json()["items"]
    assert any(x["id"] == cursor["id"] for x in open_list)
    assert c.get(f"/api/chambers/{chamber_id}", headers=h).json()["open_excursion_count"] >= 1
    batch_detail = c.get(f"/api/batches/{batch['id']}", headers=h).json()
    assert batch_detail["open_excursion_count"] == 1
    proof = c.get(f"/api/proof/batch/{batch['batch_code']}", headers=h).json()
    assert proof["open_excursions"] == 1 and proof["excursion_closed"] is False
    dash_before = c.get("/api/dashboard", headers=h).json()["open_excursions"]

    r = c.post(
        "/api/excursions/close",
        headers=h,
        json={"cursor_id": cursor["id"], "note": "贯通收口"},
    )
    assert r.status_code == 200

    # 关后：游标视图 / 仓间视图 / 批次视图 / 温谱（批次互证）视图 / 对账视图 五处对齐
    open_after = c.get("/api/excursions?status=开放", headers=h).json()["items"]
    assert all(x["id"] != cursor["id"] for x in open_after)

    chamber_after = c.get(f"/api/chambers/{chamber_id}", headers=h).json()
    assert chamber_after["open_excursion_count"] == _occupancy_open_count(chamber_id)

    batch_after = c.get(f"/api/batches/{batch['id']}", headers=h).json()
    assert batch_after["open_excursion_count"] == 0

    proof_after = c.get(f"/api/proof/batch/{batch['batch_code']}", headers=h).json()
    assert proof_after["open_excursions"] == 0
    assert proof_after["excursion_closed"] is True

    recon = c.get(f"/api/reconcile/{chamber_code}", headers=h).json()
    assert recon["open_excursion_count"] == chamber_after["open_excursion_count"]

    # 温谱视图（温谱服务读数面）开放计数同源
    spectrum_view = c.get("/api/spectrum", headers=h).json()
    assert spectrum_view["open_excursions"] == len(open_after)

    dash_after = c.get("/api/dashboard", headers=h).json()["open_excursions"]
    assert dash_after == dash_before - 1
    assert dash_after == len(open_after)

    # 温谱视图贯通：收口后可封批并导出，互证仍为零开放
    seal = c.post("/api/seals", headers=h, json={"batch_id": batch["id"]})
    assert seal.status_code == 200, seal.text
    sp = c.post("/api/spectrum/export", headers=h, json={"batch_id": batch["id"]})
    assert sp.status_code == 200, sp.text
    final_proof = c.get(f"/api/proof/batch/{batch['batch_code']}", headers=h).json()
    assert final_proof["spectrum_matches_seal"] is True
    assert final_proof["open_excursions"] == 0


def test_green_reopen_after_close_keeps_count_balanced():
    """同批次可再开新游标（旧游标已闭），计数增减始终与游标态一致。"""
    c = TestClient(app)
    h = _auth(c)
    batch, cursor, chamber_id = _open_seed_cursor(c, h, code="TB-FIX03-005")
    base = _chamber_open_count(chamber_id)

    assert c.post(
        "/api/excursions/close", headers=h, json={"cursor_id": cursor["id"]}
    ).status_code == 200
    assert _occupancy_open_count(chamber_id) == base - 1

    second = c.post(
        "/api/excursions/open",
        headers=h,
        json={"batch_id": batch["id"], "threshold": 7.0},
    )
    assert second.status_code == 200, second.text
    assert _occupancy_open_count(chamber_id) == base
    detail = c.get(f"/api/chambers/{chamber_id}", headers=h).json()
    assert detail["open_excursion_count"] == base
    assert c.post(
        "/api/excursions/close",
        headers=h,
        json={"cursor_id": second.json()["id"]},
    ).status_code == 200
    assert _occupancy_open_count(chamber_id) == base - 1
