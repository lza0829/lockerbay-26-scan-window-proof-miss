"""回收扫描窗口证明：本地夹具端到端核证（SQLite，隔离临时库）。

跑法（在 backend 目录）：
  /workspace/.venv/bin/python -I reclaim_scan_window_check.py

覆盖：
1. 首批注入中途失败 -> 整批回滚：水位不抬、reclaim_scan_window 为空（无窗口残骸）；
2. 首批成功 -> 窗口证明同批固化：from=None、to=scan_now_1、成员=A 组 3 单且均落在窗口内；
3. 同刻空批 -> 空窗口（from==to、0 成员）照常固化，窗口链不断；
4. scan_now_2 二批 -> 窗口 from=scan_now_1、to=scan_now_2、成员=B 组 2 单，链连续；
5. 每趟窗口可查：window_for_batch 逐趟取回且成员∈窗口；
6. 只读核证 window_proof：窗口与水位互证、台账/关账/占用账三方对齐，ok=True；
7. 时钟回拨 -> 空窗 from==to 不回退，链仍连续；
8. 篡改检出（SAVEPOINT 内，验后回滚）：成员改出窗口 / 删窗口行 / 失败批塞窗口，均 ok=False；
9. 成员∈窗口守卫直测：越界成员抛错，合法成员放行。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="lockerbay-window-")
os.environ["DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "reclaim_watermark_fixture.json"

from app.db import connect  # noqa: E402
from app.seed import SCHEMA  # noqa: E402
from app.services import reclaim as svc  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def load_fixture(conn, fx):
    for cab in fx["cabinets"]:
        conn.execute("INSERT INTO cabinets(id,label,site) VALUES (?,?,?)",
                     (cab["id"], cab["label"], cab["site"]))
    for cell in fx["cells"]:
        conn.execute("INSERT INTO cells(id,cabinet_id,code,size_code,status) VALUES (?,?,?,?,'occupied')",
                     (cell["id"], cell["cabinet_id"], cell["code"], cell.get("size_code", "M")))
    for occ in fx["occupancies"]:
        conn.execute(
            "INSERT INTO occupancies(id,cell_id,tracking_no,opened_at,closed_at,status,last_event_id) VALUES (?,?,?,?,NULL,'occupied',NULL)",
            (occ["id"], occ["cell_id"], occ["tracking_no"], occ["opened_at"]),
        )
        conn.execute(
            "INSERT INTO occupancy_proofs(occupancy_id,cell_id,tracking_no,opened_at,closed_at,event_ids_json,created_at) VALUES (?,?,?,?,NULL,?,?)",
            (occ["id"], occ["cell_id"], occ["tracking_no"], occ["opened_at"],
             json.dumps([f"DROP-{occ['id']}"]), fx["scan_now_1"]),
        )
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('overdue_hours', ?)",
                 (str(fx["overdue_hours"]),))
    conn.commit()


def main() -> int:
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scanner = fx["scanner"]
    t1 = svc._parse(fx["scan_now_1"])
    t2 = svc._parse(fx["scan_now_2"])
    a_ids = sorted(o["id"] for o in fx["occupancies"] if o["group"] == "A")
    b_ids = sorted(o["id"] for o in fx["occupancies"] if o["group"] == "B")

    conn = connect()
    try:
        # 只建表不灌 seed_bay，保证本夹具独占数据库、候选集可精确断言
        conn.executescript(SCHEMA)
        load_fixture(conn, fx)

        print("== 1. 首批中途失败：水位不抬、窗口不留残骸 ==")
        w0 = svc.latest_window(conn, scanner)
        check(w0["complete"] is False and w0["member_ids"] == [] and w0["from_watermark"] is None,
              "尚未成功批：末趟窗口为空态（complete=False）")
        r_fail = svc.run_scan(conn, scanner=scanner, now=t1, fail_on="OCC-T-A02")
        failed_batch = r_fail["batch_id"]
        check(r_fail["status"] == "failed" and r_fail["window"] is None,
              "失败批返回 window=None")
        check(svc.get_watermark(conn, scanner) is None, "水位行不存在（失败不抬水位）")
        cnt = conn.execute("SELECT COUNT(*) c FROM reclaim_scan_window").fetchone()["c"]
        check(cnt == 0, "reclaim_scan_window 为空：失败批无窗口残骸")
        check(svc.window_for_batch(conn, failed_batch) is None,
              "失败批按批号查窗口 -> None（无半截窗口）")

        print("== 2. 首批成功：窗口证明与水位同批固化 ==")
        r1 = svc.run_scan(conn, scanner=scanner, now=t1)
        b1 = r1["batch_id"]
        check(r1["status"] == "committed", "返回 committed")
        check(r1["window"] is not None
              and r1["window"]["from_watermark"] is None
              and r1["window"]["to_watermark"] == fx["scan_now_1"]
              and sorted(r1["window"]["member_ids"]) == a_ids,
              "扫描报告携带本趟窗口（from=None, to=scan_now_1, 成员=A 组）")
        w1 = svc.window_for_batch(conn, b1)
        check(w1 is not None and w1["from_watermark"] is None
              and w1["to_watermark"] == fx["scan_now_1"],
              "窗口落库可查：from=None（首批下界 -∞）、to=scan_now_1")
        check(sorted(w1["member_ids"]) == a_ids and w1["member_count"] == 3,
              "窗口成员快照=A 组 3 单")
        check(w1["members_in_window"] is True, "A 组成员均落在 (-∞, scan_now_1] 内")
        check(w1["cutoff_ts"] == "2026-11-18T12:00:00Z" and w1["overdue_hours"] == 48,
              "窗口固化本趟超时闸临界（to-48h）")
        led = conn.execute("SELECT prev_watermark, next_watermark FROM reclaim_batches WHERE batch_id=?",
                           (b1,)).fetchone()
        check(led["prev_watermark"] == w1["from_watermark"] and led["next_watermark"] == w1["to_watermark"],
              "窗口 from/to 与批台账 prev/next 一致")

        print("== 3. 同刻空批：空窗口照常固化、链不断 ==")
        r1b = svc.run_scan(conn, scanner=scanner, now=t1)
        w1b = svc.window_for_batch(conn, r1b["batch_id"])
        check(r1b["closed_count"] == 0 and w1b["member_count"] == 0
              and w1b["from_watermark"] == fx["scan_now_1"] and w1b["to_watermark"] == fx["scan_now_1"],
              "空窗口 from==to、0 成员照常固化")
        check(w1b["members_in_window"] is True, "空窗口成员校验 vacuously 通过")

        print("== 4. 二批超时：窗口链连续、成员∈窗口 ==")
        r2 = svc.run_scan(conn, scanner=scanner, now=t2)
        b2 = r2["batch_id"]
        w2 = svc.window_for_batch(conn, b2)
        check(w2["from_watermark"] == fx["scan_now_1"] and w2["to_watermark"] == fx["scan_now_2"],
              "二批窗口 from=scan_now_1、to=scan_now_2（接上前趟）")
        check(sorted(w2["member_ids"]) == b_ids and w2["members_in_window"] is True,
              "B 组 2 单均落在 (scan_now_1, scan_now_2] 内")
        check(all(m["reclaimed_at"] == fx["scan_now_2"] for m in w2["members"]),
              "成员关账时刻落在窗口内（== to）")

        print("== 5. 每趟窗口可查 ==")
        wins = svc.list_windows(conn, scanner)
        check([w["batch_id"] for w in wins] == [b1, r1b["batch_id"], b2],
              "窗口链按提交顺序逐趟可查（3 趟）")
        check(all(w["members_in_window"] for w in wins), "每趟成员均 ∈ 各自窗口")
        lat = svc.latest_window(conn, scanner)
        check(lat["complete"] is True and lat["batch_id"] == b2
              and sorted(lat["member_ids"]) == b_ids,
              "末趟窗口=二批（回收页窗口区有数）")

        print("== 6. 只读核证 window_proof：互证 + 三方对账 ==")
        proof = svc.window_proof(conn, scanner)
        check(proof["ok"] is True, "核证总体 ok")
        by_name = {c["name"]: c for c in proof["checks"]}
        check(all(c["ok"] for c in proof["checks"]), "逐项检查全过")
        check(proof["watermark"] == fx["scan_now_2"] and proof["last_batch_id"] == b2,
              "水位行与末趟窗口互证一致")
        check(by_name["watermark_window_sync"]["ok"]
              and by_name["window_chain_contiguous"]["ok"]
              and by_name["members_in_window"]["ok"]
              and by_name["committed_batches_have_window"]["ok"]
              and by_name["no_failed_batch_window"]["ok"]
              and by_name["occupancy_ledger_aligned"]["ok"],
              "关键项：水位互证/链连续/成员∈窗口/committed 有窗/失败无窗/占用对齐")

        print("== 7. 时钟回拨：空窗不回退、链仍连续 ==")
        old = svc._parse("2020-01-01T00:00:00Z")
        r3 = svc.run_scan(conn, scanner=scanner, now=old)
        w3 = svc.window_for_batch(conn, r3["batch_id"])
        check(w3["from_watermark"] == fx["scan_now_2"] and w3["to_watermark"] == fx["scan_now_2"]
              and w3["member_count"] == 0,
              "回拨批固化空窗 from==to==历史水位，不回退")
        proof = svc.window_proof(conn, scanner)
        check(proof["ok"] is True and proof["window_count"] == 4,
              "回拨后核证仍 ok（4 趟窗口链连续）")

        print("== 8. 篡改检出（SAVEPOINT 内，验后回滚） ==")
        conn.execute("SAVEPOINT t_amp_member")
        conn.execute("UPDATE reclaim_events SET opened_at='2099-01-01T00:00:00Z' WHERE occupancy_id='OCC-T-B01'")
        p = svc.window_proof(conn, scanner)
        check(p["ok"] is False
              and not {c["name"]: c for c in p["checks"]}["members_in_window"]["ok"],
              "成员 opened_at 改出窗口 -> members_in_window 指认")
        conn.execute("ROLLBACK TO t_amp_member")
        conn.execute("RELEASE t_amp_member")

        conn.execute("SAVEPOINT t_drop_window")
        conn.execute("DELETE FROM reclaim_scan_window WHERE batch_id=?", (b2,))
        p = svc.window_proof(conn, scanner)
        names = {c["name"]: c for c in p["checks"]}
        check(p["ok"] is False and not names["committed_batches_have_window"]["ok"],
              "删掉 committed 批窗口 -> 水位已抬窗口证明缺被检出")
        conn.execute("ROLLBACK TO t_drop_window")
        conn.execute("RELEASE t_drop_window")

        conn.execute("SAVEPOINT t_failed_window")
        conn.execute(
            "INSERT INTO reclaim_scan_window(batch_id, scanner_name, from_watermark, to_watermark, cutoff_ts, overdue_hours, member_count, member_ids_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (failed_batch, scanner, None, fx["scan_now_1"], "2026-11-18T12:00:00Z", 48, 0, "[]", fx["scan_now_1"]),
        )
        p = svc.window_proof(conn, scanner)
        names = {c["name"]: c for c in p["checks"]}
        check(p["ok"] is False and not names["no_failed_batch_window"]["ok"],
              "失败批塞入窗口 -> 窗口残骸被检出")
        conn.execute("ROLLBACK TO t_failed_window")
        conn.execute("RELEASE t_failed_window")
        check(svc.window_proof(conn, scanner)["ok"] is True, "篡改回滚后核证恢复 ok")

        print("== 9. 成员∈窗口守卫直测 ==")
        try:
            svc._assert_members_in_window(
                fx["scan_now_1"], fx["scan_now_2"], "2026-11-20T12:00:00Z",
                [{"id": "OCC-X", "opened_at": "2026-11-23T00:00:00Z", "reclaimed_at": fx["scan_now_2"]}])
            raised = False
        except RuntimeError:
            raised = True
        check(raised, "opened_at 越出窗口上界 -> 守卫抛错（禁止窗口外成员成功）")
        try:
            svc._assert_members_in_window(
                fx["scan_now_1"], fx["scan_now_2"], "2026-11-20T12:00:00Z",
                [{"id": "OCC-Y", "opened_at": "2026-11-19T00:00:00Z", "reclaimed_at": fx["scan_now_1"]}])
            raised = False
        except RuntimeError:
            raised = True
        check(raised, "关账时刻落在 (from, to] 外 -> 守卫抛错")
        try:
            svc._assert_members_in_window(
                fx["scan_now_1"], fx["scan_now_2"], "2026-11-20T12:00:00Z",
                [{"id": "OCC-Z", "opened_at": "2026-11-19T08:00:00Z", "reclaimed_at": fx["scan_now_2"]}])
            raised = True
        except RuntimeError:
            raised = False
        check(raised, "合法成员（B 组形态）放行不抛错")
    finally:
        conn.close()

    print()
    if failures:
        print(f"FAILED {len(failures)} 项: {failures}")
        return 1
    print("ALL PASS —— 窗口证明与水位同成败固化，成员∈窗口，失败不留残骸，每趟可查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
