"""回收扫描水位：本地夹具端到端核证（SQLite，隔离临时库）。

跑法（在 backend 目录）：
  /workspace/.venv/bin/python -I reclaim_watermark_check.py

覆盖：
1. 首批注入中途失败 -> 整批回滚：占用仍 open、格口仍 occupied、水位不抬、失败台账 next_watermark=NULL；
2. 同刻重跑成功 -> A 组 3 单全员关账，水位前进到 scan_now_1，无重复关账；
3. 同刻再扫 -> 0 关闭，幂等；
4. scan_now_2 再扫 -> B 组 2 单超时后被收，水位单调前进，共 5 单无漏无重；
5. 只读核证 watermark_proof：水位 / 本批关闭数 / 成员与台账、占用账三方对得上。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="lockerbay-reclaim-")
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
    all_ids = [o["id"] for o in fx["occupancies"]]
    a_ids = [o["id"] for o in fx["occupancies"] if o["group"] == "A"]
    b_ids = [o["id"] for o in fx["occupancies"] if o["group"] == "B"]

    conn = connect()
    try:
        # 只建表不灌 seed_bay，保证本夹具独占数据库、候选集可精确断言
        conn.executescript(SCHEMA)
        load_fixture(conn, fx)

        print("== 1. 首批中途失败（fail_on=OCC-T-A02），失败闭回滚 ==")
        r = svc.run_scan(conn, scanner=scanner, now=t1, fail_on="OCC-T-A02")
        check(r["status"] == "failed", "返回 failed")
        check(r["closed_count"] == 0 and r["next_watermark"] is None, "失败批关闭数 0 且不抬水位")
        check(svc.get_watermark(conn, scanner) is None, "水位行不存在（禁止失败批前进水位）")
        open_rows = conn.execute(
            "SELECT id FROM occupancies WHERE status='occupied' ORDER BY id"
        ).fetchall()
        check([x["id"] for x in open_rows] == sorted(all_ids), "5 单占用全部仍 open（无漏关/无半关）")
        busy = conn.execute("SELECT COUNT(*) c FROM cells WHERE status='occupied'").fetchone()["c"]
        check(busy == 5, "5 个格口仍 occupied")
        check(conn.execute("SELECT COUNT(*) c FROM reclaim_events").fetchone()["c"] == 0,
              "成员留痕随事务回滚，reclaim_events 为空")
        fb = conn.execute("SELECT * FROM reclaim_batches WHERE status='failed'").fetchone()
        check(fb is not None and fb["next_watermark"] is None and fb["closed_count"] == 0,
              "失败台账留存且 next_watermark 恒 NULL")

        print("== 2. 同刻重跑成功：A 组 3 单原子关账 + 水位前进 ==")
        r = svc.run_scan(conn, scanner=scanner, now=t1)
        check(r["status"] == "committed", "返回 committed")
        check(r["closed_count"] == 3, "本批关闭数=3")
        check(sorted(r["members"]) == sorted(a_ids), "成员恰为 A 组 3 单")
        check(svc.get_watermark(conn, scanner) == fx["scan_now_1"],
              f"水位前进到 {fx['scan_now_1']}")
        reclaimed = conn.execute(
            "SELECT id FROM occupancies WHERE status='reclaimed' ORDER BY id"
        ).fetchall()
        check([x["id"] for x in reclaimed] == sorted(a_ids), "A 组 3 单状态 reclaimed")
        still_open = conn.execute(
            "SELECT id FROM occupancies WHERE status='occupied' ORDER BY id"
        ).fetchall()
        check([x["id"] for x in still_open] == sorted(b_ids), "B 组 2 单仍 open（未超时不处理）")
        free_cells = conn.execute(
            "SELECT id FROM cells WHERE status='free' ORDER BY id"
        ).fetchall()
        check([x["id"] for x in free_cells] == ["CAB-T-01", "CAB-T-02", "CAB-T-03"],
              "A 组格口已释放")
        dup = conn.execute(
            "SELECT occupancy_id, COUNT(*) c FROM reclaim_events GROUP BY occupancy_id HAVING c>1"
        ).fetchall()
        check(dup == [], "无重复关账成员")

        print("== 3. 同刻再扫：幂等，0 关闭 ==")
        r = svc.run_scan(conn, scanner=scanner, now=t1)
        check(r["status"] == "committed" and r["closed_count"] == 0, "空批成功且 0 关闭")
        check(svc.get_watermark(conn, scanner) == fx["scan_now_1"], "水位值不回退")

        print("== 4. scan_now_2 再扫：B 组超时入批，水位单调前进 ==")
        r = svc.run_scan(conn, scanner=scanner, now=t2)
        check(r["status"] == "committed" and r["closed_count"] == 2, "本批关闭数=2")
        check(sorted(r["members"]) == sorted(b_ids), "成员恰为 B 组 2 单")
        check(svc.get_watermark(conn, scanner) == fx["scan_now_2"],
              f"水位前进到 {fx['scan_now_2']}（单调）")
        left_open = conn.execute(
            "SELECT COUNT(*) c FROM occupancies WHERE status='occupied'"
        ).fetchone()["c"]
        check(left_open == 0, "截至 scan_now_2 无超时占用残留（无漏关）")
        total_ev = conn.execute("SELECT COUNT(*) c FROM reclaim_events").fetchone()["c"]
        check(total_ev == 5, "成员留痕共 5 条（无重关）")

        print("== 5. 只读核证 watermark_proof 三方对账 ==")
        proof = svc.watermark_proof(conn, scanner)
        check(proof["watermark"] == fx["scan_now_2"], "核证水位=当前水位")
        check(proof["last_batch_id"] is not None and proof["last_closed_count"] == 2,
              "核证本批关闭数=2")
        check(sorted(m["occupancy_id"] for m in proof["members"]) == sorted(b_ids),
              "核证成员占用=B 组 2 单")
        check(all(m["reclaimed_at"] == fx["scan_now_2"] for m in proof["members"]),
              "成员回收时刻与本批一致")
        statuses = [b["status"] for b in proof["recent_batches"]]
        check(statuses.count("failed") == 1 and statuses.count("committed") == 3,
              "批台账留痕：1 失败 + 3 成功（含空批）")
        # 全局不变量：每个 reclaimed 占用都能在 committed 批的成员表中找到，且 closed_at 非空
        orphan = conn.execute(
            "SELECT o.id FROM occupancies o WHERE o.status='reclaimed' "
            "AND (o.closed_at IS NULL OR NOT EXISTS "
            "(SELECT 1 FROM reclaim_events e JOIN reclaim_batches b ON e.batch_id=b.batch_id "
            " WHERE e.occupancy_id=o.id AND b.status='committed'))"
        ).fetchall()
        check(orphan == [], "无「水位已过但占用仍 open」之外的孤儿：关账必有 committed 成员留痕")
        # 失败批不得出现在成员表
        bad = conn.execute(
            "SELECT 1 FROM reclaim_events e JOIN reclaim_batches b ON e.batch_id=b.batch_id WHERE b.status='failed'"
        ).fetchall()
        check(bad == [], "失败批无任何成员落库")

        print("== 6. 占用核证表 occupancy_proofs 回收边同源 ==")
        pr = conn.execute(
            "SELECT closed_at, event_ids_json FROM occupancy_proofs WHERE occupancy_id='OCC-T-A01'"
        ).fetchone()
        ids = json.loads(pr["event_ids_json"])
        check(pr["closed_at"] == fx["scan_now_1"] and ids[-1].startswith("RC-EVT-"),
              "回收边写回占用核证（与 reclaim_events 同源事件号）")

        print("== 7. 时钟回拨：成功批水位只许前进不许回退 ==")
        before = svc.get_watermark(conn, scanner)
        old = svc._parse("2020-01-01T00:00:00Z")
        r = svc.run_scan(conn, scanner=scanner, now=old)
        check(r["status"] == "committed" and r["next_watermark"] == before,
              "now 早于历史水位时，新水位保持历史值不回退")
        check(svc.get_watermark(conn, scanner) == before, "落库水位未回退")
    finally:
        conn.close()

    print()
    if failures:
        print(f"FAILED {len(failures)} 项: {failures}")
        return 1
    print("ALL PASS —— 水位与占用账一致，失败批不抬水位、不半关。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
