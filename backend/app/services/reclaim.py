"""超时回收扫描：持久水位 + 单事务关账。

定死的规则：
1. 水位形态择一 —— 独立 reclaim_scan_watermarks 表按扫描器名记录（非内存、非 settings 键）。
   本批只处理 opened_at <= 本批水位 且已超时的占用；成功后水位前进，失败不抬水位。
2. 失败闭（路径择一）—— 本批关账与水位更新在同一事务：
   关账（occupancies/cells/proofs/events）+ 水位推进 + 批台账 一次 commit；
   任何成员失败即 rollback，占用仍 open、水位不动、批台账标 failed。
   杜绝「水位已过但占用仍 open」与「占用已关但水位未动」。
3. 核证只读：watermark_proof() 给出水位、本批关闭数、成员占用。
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

DEFAULT_SCANNER = "overdue-reclaim-scanner"
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, TS_FMT).replace(tzinfo=timezone.utc)


def _get_watermark_row(conn, scanner: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reclaim_scan_watermarks WHERE scanner_name=?", (scanner,)
    ).fetchone()


def get_watermark(conn, scanner: str = DEFAULT_SCANNER) -> str | None:
    row = _get_watermark_row(conn, scanner)
    return row["watermark"] if row else None


def _overdue_hours(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='overdue_hours'").fetchone()
    return int(row["value"]) if row else 48


def select_due(conn, now: datetime, overdue_hours: int, watermark: str) -> list[sqlite3.Row]:
    """本批候选：仍 occupied、已超时（opened_at <= now-overdue_hours）、
    且 opened_at <= 本批水位 —— 三条同时满足才进本批，定死不跳扫。"""
    cutoff = _fmt(now - timedelta(hours=overdue_hours))
    return conn.execute(
        "SELECT o.* FROM occupancies o "
        "WHERE o.status='occupied' AND o.opened_at <= ? AND o.opened_at <= ? "
        "ORDER BY o.opened_at, o.id",
        (cutoff, watermark),
    ).fetchall()


def run_scan(
    conn,
    scanner: str = DEFAULT_SCANNER,
    now: datetime | None = None,
    fail_on: str | None = None,
):
    """跑一批回收扫描。

    fail_on: 夹具注入 —— 处理到该 occupancy_id 时抛错，验证失败批整体回滚
             （水位不前进、占用仍 open、无重关/漏关）。
    返回批报告 dict；无论成功失败都落 reclaim_batches 台账（失败台账在回滚后单独提交）。
    """
    now = now or _now()
    now_ts = _fmt(now)
    overdue_hours = _overdue_hours(conn)
    batch_id = f"RC-{uuid4().hex[:8]}"

    wm_row = _get_watermark_row(conn, scanner)
    prev_watermark = wm_row["watermark"] if wm_row else None
    # 本批水位定死取当前时刻：只处理 opened_at <= 水位 且已超时的占用；
    # 水位只随成功批前进，失败批回滚后 prev 原样保留，重跑不会丢审计区间。
    # 时钟回拨保护：成功批水位只许前进，now 早于历史水位时保持历史水位不回退。
    target_watermark = now_ts
    if prev_watermark is not None and target_watermark < prev_watermark:
        target_watermark = prev_watermark

    due = select_due(conn, now, overdue_hours, target_watermark)
    # sqlite3.Row 无 .get()，显式取列转 dict，供失败台账记录尝试成员
    members = [
        {
            "id": r["id"],
            "cell_id": r["cell_id"],
            "tracking_no": r["tracking_no"],
            "opened_at": r["opened_at"],
        }
        for r in due
    ]

    try:
        # —— 单事务：关账 + 成员留痕 + 水位前进 + 成功台账，全部同生共死 ——
        conn.execute("BEGIN IMMEDIATE")
        closed = 0
        for occ in due:
            if fail_on and occ["id"] == fail_on:
                raise RuntimeError(f"夹具注入失败：成员 {occ['id']} 关账中途出错")
            reclaimed_at = now_ts
            event_id = f"RC-EVT-{uuid4().hex[:8]}"
            cur = conn.execute(
                "UPDATE occupancies SET closed_at=?, status='reclaimed', last_event_id=? WHERE id=? AND status='occupied'",
                (reclaimed_at, event_id, occ["id"]),
            )
            if cur.rowcount == 0:
                raise RuntimeError(f"占用 {occ['id']} 已被并发关闭，拒绝重复关账")
            conn.execute("UPDATE cells SET status='free' WHERE id=?", (occ["cell_id"],))
            conn.execute(
                "INSERT INTO reclaim_events(batch_id, occupancy_id, cell_id, tracking_no, opened_at, reclaimed_at, event_id) VALUES (?,?,?,?,?,?,?)",
                (
                    batch_id,
                    occ["id"],
                    occ["cell_id"],
                    occ["tracking_no"],
                    occ["opened_at"],
                    reclaimed_at,
                    event_id,
                ),
            )
            proof_row = conn.execute(
                "SELECT id, event_ids_json FROM occupancy_proofs WHERE occupancy_id=? ORDER BY id DESC LIMIT 1",
                (occ["id"],),
            ).fetchone()
            if proof_row:
                ids = json.loads(proof_row["event_ids_json"])
                ids.append(event_id)
                conn.execute(
                    "UPDATE occupancy_proofs SET closed_at=?, event_ids_json=? WHERE id=?",
                    (reclaimed_at, json.dumps(ids, ensure_ascii=False), proof_row["id"]),
                )
            closed += 1

        # 水位前进：只许前进不许回退
        if prev_watermark is None:
            conn.execute(
                "INSERT INTO reclaim_scan_watermarks(scanner_name, watermark, advanced_at, last_batch_id, last_closed_count) VALUES (?,?,?,?,?)",
                (scanner, target_watermark, now_ts, batch_id, closed),
            )
        else:
            cur = conn.execute(
                "UPDATE reclaim_scan_watermarks SET watermark=?, advanced_at=?, last_batch_id=?, last_closed_count=? WHERE scanner_name=? AND watermark=?",
                (target_watermark, now_ts, batch_id, closed, scanner, prev_watermark),
            )
            if cur.rowcount == 0:
                raise RuntimeError("水位行被并发改动，乐观锁失败，整批回滚")

        conn.execute(
            "INSERT INTO reclaim_batches(batch_id, scanner_name, started_at, finished_at, prev_watermark, next_watermark, closed_count, status, detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                scanner,
                now_ts,
                now_ts,
                prev_watermark,
                target_watermark,
                closed,
                "committed",
                json.dumps({"members": [m["id"] for m in members]}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return {
            "batch_id": batch_id,
            "status": "committed",
            "prev_watermark": prev_watermark,
            "next_watermark": target_watermark,
            "closed_count": closed,
            "members": [m["id"] for m in members],
        }
    except Exception as exc:
        # —— 失败闭：关账与水位整体回滚，禁止失败批偷偷抬水位 ——
        conn.rollback()
        # 失败台账单独事务落库（next_watermark 恒为 NULL，水位行原样不动）
        conn.execute(
            "INSERT INTO reclaim_batches(batch_id, scanner_name, started_at, finished_at, prev_watermark, next_watermark, closed_count, status, detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                scanner,
                now_ts,
                _fmt(_now()),
                prev_watermark,
                None,
                0,
                "failed",
                json.dumps(
                    {"error": str(exc), "attempted_members": [m["id"] for m in members]},
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
        return {
            "batch_id": batch_id,
            "status": "failed",
            "prev_watermark": prev_watermark,
            "next_watermark": None,
            "closed_count": 0,
            "error": str(exc),
            "attempted_members": [m["id"] for m in members],
        }


def watermark_proof(conn, scanner: str = DEFAULT_SCANNER) -> dict:
    """只读核证：水位、本批关闭数、成员占用。"""
    wm = _get_watermark_row(conn, scanner)
    last_batch_id = wm["last_batch_id"] if wm else None
    batch = None
    members: list = []
    if last_batch_id:
        batch = conn.execute(
            "SELECT * FROM reclaim_batches WHERE batch_id=?", (last_batch_id,)
        ).fetchone()
        members = [
            dict(r)
            for r in conn.execute(
                "SELECT occupancy_id, cell_id, tracking_no, opened_at, reclaimed_at, event_id FROM reclaim_events WHERE batch_id=? ORDER BY opened_at, occupancy_id",
                (last_batch_id,),
            ).fetchall()
        ]
    recent_batches = [
        dict(r)
        for r in conn.execute(
            "SELECT batch_id, started_at, prev_watermark, next_watermark, closed_count, status FROM reclaim_batches ORDER BY started_at DESC, batch_id DESC LIMIT 10"
        ).fetchall()
    ]
    return {
        "scanner_name": scanner,
        "watermark": wm["watermark"] if wm else None,
        "advanced_at": wm["advanced_at"] if wm else None,
        "last_batch_id": last_batch_id,
        "last_closed_count": wm["last_closed_count"] if wm else 0,
        "last_batch": dict(batch) if batch else None,
        "members": members,
        "recent_batches": recent_batches,
    }
