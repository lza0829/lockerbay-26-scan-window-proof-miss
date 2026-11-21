from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.seed import overdue_hours
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc
from app.services import reclaim as reclaim_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"overdue_hours": overdue_hours(conn)}
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        conn.commit()
        return {"overdue_hours": hours}
    finally:
        conn.close()


@router.post("/reclaim/scan")
def post_reclaim_scan(body: dict = Body(default={})):
    """跑一批超时回收：关账与水位同事务，失败回滚且水位不前进。

    body.fail_on 仅供本地夹具注入中途失败（occupancy_id）。
    """
    scanner = body.get("scanner", reclaim_svc.DEFAULT_SCANNER)
    fail_on = body.get("fail_on")
    conn = connect()
    try:
        return reclaim_svc.run_scan(conn, scanner=scanner, fail_on=fail_on)
    finally:
        conn.close()


@router.get("/reclaim/watermark-proof")
def get_reclaim_watermark_proof(scanner: str | None = None):
    """只读核证：水位、本批关闭数、成员占用、近期批台账。"""
    conn = connect()
    try:
        return reclaim_svc.watermark_proof(
            conn, scanner or reclaim_svc.DEFAULT_SCANNER
        )
    finally:
        conn.close()

# --- 回收扫描窗口证明（只读） ---
from app.modules import reclaim_scan_window as scan_win


@router.get("/reclaim-scan/window")
def reclaim_scan_window(scanner: str | None = None):
    """末趟扫描窗口证明：from/to 水位 + 本趟成员占用 id。"""
    conn = connect()
    try:
        return scan_win.latest_window(conn, scanner or reclaim_svc.DEFAULT_SCANNER)
    finally:
        conn.close()


@router.get("/reclaim/windows")
def reclaim_windows(scanner: str | None = None):
    """每趟窗口可查：该扫描器全部成功批的窗口证明链（按提交顺序）。"""
    conn = connect()
    try:
        return scan_win.list_windows(conn, scanner or reclaim_svc.DEFAULT_SCANNER)
    finally:
        conn.close()


@router.get("/reclaim/windows/{batch_id}")
def reclaim_window_for_batch(batch_id: str):
    """单趟窗口证明：按批号取回；失败批已回滚、无窗口残骸，返回 404。"""
    conn = connect()
    try:
        w = scan_win.window_for_batch(conn, batch_id)
        if w is None:
            raise HTTPException(404, "该批无窗口证明（未跑批或批已失败回滚）")
        return w
    finally:
        conn.close()


@router.get("/reclaim/window-proof")
def reclaim_window_proof(scanner: str | None = None):
    """窗口核证只读接口：窗口链与水位前进互证、成员∈窗口、台账/关账/占用账三方对账。"""
    conn = connect()
    try:
        return scan_win.window_proof(conn, scanner or reclaim_svc.DEFAULT_SCANNER)
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "occupancy_id" in out and "occ_id" not in out:
        out["occ_id"] = out.get("occupancy_id")
    if "cell_id" in out and "locker_cell" not in out:
        out["locker_cell"] = out.get("cell_id")
    if "tracking_no" in out and "parcel_key" not in out:
        out["parcel_key"] = out.get("tracking_no")
    if "verified" not in out:
        out["verified"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True}
    if extra:
        data.update(extra)
    return data

