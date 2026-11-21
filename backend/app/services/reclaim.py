"""超时回收扫描：持久水位 + 扫描窗口证明 + 单事务关账。

定死的规则：
1. 水位形态择一 —— 独立 reclaim_scan_watermarks 表按扫描器名记录（非内存、非 settings 键）。
   本批只处理 opened_at <= 本批水位 且已超时的占用；成功后水位前进，失败不抬水位。
2. 窗口落库择一 —— 独立 reclaim_scan_window 表（非水位行 window_json 快照）：
   每趟成功批固化一行窗口证明（from_watermark、to_watermark、本趟成员占用 id 快照、
   超时闸临界），重跑可审计「扫过哪段」；成员占用均落在 (from, to] 内。
3. 提交形态择一 —— 抬水位同事务写窗口证明，缺一整批回滚（非先草稿后原子抬）：
   关账（occupancies/cells/proofs/events）+ 水位推进 + 窗口证明 + 批台账 一次 commit；
   任何成员失败即 rollback，占用仍 open、水位不动、窗口不留残骸、批台账标 failed。
   杜绝「水位已过但占用仍 open」「占用已关但水位未动」「水位已抬但窗口证明缺」。
4. 成员∈窗口守卫 —— 提交前逐成员校验落在 (from, to] 内，越界即抛错整批回滚，
   禁止成员落在窗口外仍成功。
5. 核证只读：watermark_proof() 给水位、本批关闭数、成员占用；
   window_proof() 给窗口链与水位互证、成员∈窗口、台账/关账/占用账三方对账。
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


def _cutoff_ts(now: datetime, overdue_hours: int) -> str:
    """本趟超时闸临界：opened_at <= cutoff 才算超时。"""
    return _fmt(now - timedelta(hours=overdue_hours))


def select_due(conn, now: datetime, overdue_hours: int, watermark: str) -> list[sqlite3.Row]:
    """本批候选：仍 occupied、已超时（opened_at <= now-overdue_hours）、
    且 opened_at <= 本批水位 —— 三条同时满足才进本批，定死不跳扫。"""
    cutoff = _cutoff_ts(now, overdue_hours)
    return conn.execute(
        "SELECT o.* FROM occupancies o "
        "WHERE o.status='occupied' AND o.opened_at <= ? AND o.opened_at <= ? "
        "ORDER BY o.opened_at, o.id",
        (cutoff, watermark),
    ).fetchall()


def _member_in_window(from_wm: str | None, to_wm: str, cutoff_ts: str, member: dict) -> bool:
    """成员占用落在窗口 (from, to] 内的判定（水位=扫描时刻语义下唯一自洽的定义）：

    1. opened_at <= to —— 成员落在本趟扫描前缀内（本批只处理 opened_at <= 水位）；
    2. from < reclaimed_at <= to —— 关账事件落在窗口内（首批 from=None 不检下界）；
    3. opened_at <= cutoff —— 成员在本趟超时闸内。
    """
    if member["opened_at"] > to_wm:
        return False
    if from_wm is not None and not (from_wm < member["reclaimed_at"] <= to_wm):
        return False
    if member["opened_at"] > cutoff_ts:
        return False
    return True


def _assert_members_in_window(
    from_wm: str | None,
    to_wm: str,
    cutoff_ts: str,
    members: list[dict],
) -> None:
    """硬约束守卫：提交前逐成员校验落在 (from, to] 内；任一越界即抛错，
    整批回滚 —— 禁止成员落在窗口外仍成功，禁止水位前进而窗口证明缺。"""
    if from_wm is not None and to_wm < from_wm:
        raise RuntimeError(f"窗口区间倒置：from={from_wm} > to={to_wm}，拒绝固化")
    for m in members:
        if not _member_in_window(from_wm, to_wm, cutoff_ts, m):
            raise RuntimeError(
                f"成员 {m['id']} 落在窗口 ({from_wm}, {to_wm}] 外"
                f"（opened_at={m['opened_at']} reclaimed_at={m['reclaimed_at']}），"
                "禁止窗口外成员随批成功，整批回滚"
            )


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
    cutoff_ts = _cutoff_ts(now, overdue_hours)
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

        # —— 窗口证明：与关账、水位前进同事务固化，缺一整批回滚 ——
        # 成员∈窗口守卫：任一成员落在 (from, to] 外即抛错，水位不抬、窗口不留残骸
        window_members = [
            {
                "id": m["id"],
                "opened_at": m["opened_at"],
                "reclaimed_at": now_ts,
            }
            for m in members
        ]
        _assert_members_in_window(prev_watermark, target_watermark, cutoff_ts, window_members)
        conn.execute(
            "INSERT INTO reclaim_scan_window(batch_id, scanner_name, from_watermark, to_watermark, cutoff_ts, overdue_hours, member_count, member_ids_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                scanner,
                prev_watermark,
                target_watermark,
                cutoff_ts,
                overdue_hours,
                closed,
                json.dumps([m["id"] for m in members], ensure_ascii=False),
                now_ts,
            ),
        )

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
            "window": {
                "from_watermark": prev_watermark,
                "to_watermark": target_watermark,
                "cutoff_ts": cutoff_ts,
                "member_ids": [m["id"] for m in members],
            },
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
            "window": None,
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


# ————————————————————————————————————————————————————————————
# 扫描窗口证明（只读）：每趟窗口可查、成员∈窗口、窗口与水位前进互证
# ————————————————————————————————————————————————————————————


def _window_members(conn, batch_id: str) -> list[dict]:
    """窗口成员明细：与成员关账留痕 reclaim_events 同源。"""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT occupancy_id, cell_id, tracking_no, opened_at, reclaimed_at, event_id FROM reclaim_events WHERE batch_id=? ORDER BY opened_at, occupancy_id",
            (batch_id,),
        ).fetchall()
    ]


def _window_to_dict(conn, row: sqlite3.Row) -> dict:
    members = _window_members(conn, row["batch_id"])
    member_ids = json.loads(row["member_ids_json"])
    members_ok = all(
        _member_in_window(row["from_watermark"], row["to_watermark"], row["cutoff_ts"], m)
        for m in members
    )
    return {
        "batch_id": row["batch_id"],
        "scanner_name": row["scanner_name"],
        "from_watermark": row["from_watermark"],
        "to_watermark": row["to_watermark"],
        "cutoff_ts": row["cutoff_ts"],
        "overdue_hours": row["overdue_hours"],
        "member_count": row["member_count"],
        "member_ids": member_ids,
        "members": members,
        "members_in_window": members_ok,
        "created_at": row["created_at"],
    }


def latest_window(conn, scanner: str = DEFAULT_SCANNER) -> dict:
    """末趟扫描窗口证明：from/to 水位 + 本趟成员占用 id；尚无成功批时 complete=False。"""
    row = conn.execute(
        "SELECT * FROM reclaim_scan_window WHERE scanner_name=? ORDER BY rowid DESC LIMIT 1",
        (scanner,),
    ).fetchone()
    if not row:
        return {
            "scanner_name": scanner,
            "batch_id": None,
            "from_watermark": None,
            "to_watermark": None,
            "cutoff_ts": None,
            "member_count": 0,
            "member_ids": [],
            "members": [],
            "members_in_window": False,
            "complete": False,
        }
    w = _window_to_dict(conn, row)
    w["complete"] = True
    return w


def list_windows(conn, scanner: str = DEFAULT_SCANNER) -> list[dict]:
    """每趟窗口可查：按提交顺序返回该扫描器全部成功批的窗口证明链。"""
    rows = conn.execute(
        "SELECT * FROM reclaim_scan_window WHERE scanner_name=? ORDER BY rowid",
        (scanner,),
    ).fetchall()
    return [_window_to_dict(conn, r) for r in rows]


def window_for_batch(conn, batch_id: str) -> dict | None:
    """单趟窗口证明：按批号取回；未跑批或批已失败回滚时返回 None。"""
    row = conn.execute(
        "SELECT * FROM reclaim_scan_window WHERE batch_id=?", (batch_id,)
    ).fetchone()
    if not row:
        return None
    w = _window_to_dict(conn, row)
    w["complete"] = True
    return w


def window_proof(conn, scanner: str = DEFAULT_SCANNER) -> dict:
    """窗口核证只读接口：窗口链与水位前进互证、成员∈窗口、台账/关账/占用账三方对账。

    逐项检查（任一不过 ok=False）：
    - window_range_valid：每趟 to >= from；
    - members_in_window：每趟成员占用均落在 (from, to] 内；
    - member_snapshot_matches_events：窗口成员快照与关账留痕一致；
    - window_chain_contiguous：首趟 from=None，其后每趟 from == 前趟 to（扫过哪段无缺口）；
    - watermark_window_sync：水位行值 == 末趟 to、last_batch_id == 末趟批号；无水位则无窗口；
    - committed_batches_have_window：每个 committed 批必有窗口（禁止水位已抬窗口证明缺）；
    - no_failed_batch_window：失败批无任何窗口（禁止失败回滚留窗口残骸）；
    - window_matches_ledger：窗口 from/to 与批台账 prev/next 一致，且无无台账窗口；
    - occupancy_ledger_aligned：窗口成员的占用账均 reclaimed 且 closed_at 与关账时刻一致。
    """
    wm = _get_watermark_row(conn, scanner)
    windows = list_windows(conn, scanner)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    range_bad = [
        w["batch_id"]
        for w in windows
        if w["from_watermark"] is not None and w["to_watermark"] < w["from_watermark"]
    ]
    add(
        "window_range_valid",
        not range_bad,
        "每趟窗口 to >= from" if not range_bad else f"窗口区间倒置：{range_bad}",
    )

    member_bad = {
        w["batch_id"]: [
            m["occupancy_id"]
            for m in w["members"]
            if not _member_in_window(w["from_watermark"], w["to_watermark"], w["cutoff_ts"], m)
        ]
        for w in windows
    }
    member_bad = {b: ids for b, ids in member_bad.items() if ids}
    add(
        "members_in_window",
        not member_bad,
        "每趟成员占用均落在 (from, to] 内" if not member_bad else f"窗口外成员：{member_bad}",
    )

    snapshot_bad = [
        w["batch_id"]
        for w in windows
        if sorted(m["occupancy_id"] for m in w["members"]) != sorted(w["member_ids"])
        or len(w["member_ids"]) != w["member_count"]
    ]
    add(
        "member_snapshot_matches_events",
        not snapshot_bad,
        "窗口成员快照与关账留痕一致" if not snapshot_bad else f"快照与留痕不符：{snapshot_bad}",
    )

    chain_bad = []
    prev_to = None
    for i, w in enumerate(windows):
        if i == 0:
            if w["from_watermark"] is not None:
                chain_bad.append(w["batch_id"])
        elif w["from_watermark"] != prev_to:
            chain_bad.append(w["batch_id"])
        prev_to = w["to_watermark"]
    add(
        "window_chain_contiguous",
        not chain_bad,
        "窗口链连续：每趟 from == 前趟 to" if not chain_bad else f"窗口链断档：{chain_bad}",
    )

    if wm is None:
        add(
            "watermark_window_sync",
            not windows,
            "尚无成功批：水位与窗口同时为空" if not windows else "无水位行却存在窗口证明",
        )
    else:
        latest = windows[-1] if windows else None
        sync_ok = (
            latest is not None
            and wm["watermark"] == latest["to_watermark"]
            and wm["last_batch_id"] == latest["batch_id"]
        )
        add(
            "watermark_window_sync",
            sync_ok,
            "水位行与末趟窗口互证一致"
            if sync_ok
            else "水位行与末趟窗口不符（水位已抬窗口证明缺或末批错位）",
        )

    committed = {
        r["batch_id"]: r
        for r in conn.execute(
            "SELECT batch_id, prev_watermark, next_watermark FROM reclaim_batches WHERE scanner_name=? AND status='committed'",
            (scanner,),
        ).fetchall()
    }
    failed_ids = {
        r["batch_id"]
        for r in conn.execute(
            "SELECT batch_id FROM reclaim_batches WHERE scanner_name=? AND status='failed'",
            (scanner,),
        ).fetchall()
    }
    window_ids = {w["batch_id"] for w in windows}

    missing = sorted(b for b in committed if b not in window_ids)
    add(
        "committed_batches_have_window",
        not missing,
        "每个 committed 批都有窗口证明" if not missing else f"水位已抬窗口证明缺：{missing}",
    )

    remnant = sorted(b for b in window_ids if b in failed_ids)
    add(
        "no_failed_batch_window",
        not remnant,
        "失败批无窗口残骸" if not remnant else f"失败批留下窗口残骸：{remnant}",
    )

    ledger_bad = [
        w["batch_id"]
        for w in windows
        if w["batch_id"] not in committed
        or committed[w["batch_id"]]["prev_watermark"] != w["from_watermark"]
        or committed[w["batch_id"]]["next_watermark"] != w["to_watermark"]
    ]
    add(
        "window_matches_ledger",
        not ledger_bad,
        "窗口与批台账 prev/next 水位一致" if not ledger_bad else f"窗口与台账不符：{ledger_bad}",
    )

    occ_bad = []
    for w in windows:
        for m in w["members"]:
            occ = conn.execute(
                "SELECT status, closed_at FROM occupancies WHERE id=?", (m["occupancy_id"],)
            ).fetchone()
            if not occ or occ["status"] != "reclaimed" or occ["closed_at"] != m["reclaimed_at"]:
                occ_bad.append(f"{w['batch_id']}:{m['occupancy_id']}")
    add(
        "occupancy_ledger_aligned",
        not occ_bad,
        "窗口成员占用账均 reclaimed 且时刻一致" if not occ_bad else f"占用账不符：{occ_bad}",
    )

    return {
        "scanner_name": scanner,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "watermark": wm["watermark"] if wm else None,
        "advanced_at": wm["advanced_at"] if wm else None,
        "last_batch_id": wm["last_batch_id"] if wm else None,
        "window_count": len(windows),
        "latest_window": windows[-1] if windows else None,
        "windows": windows,
    }
