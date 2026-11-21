"""回收扫描窗口证明（功能题实现）。

实现位于 app.services.reclaim：
- 落库形态择一：独立 reclaim_scan_window 表（每趟成功批一行：from/to 水位 +
  本趟成员占用 id 快照 + 超时闸临界），非水位行 window_json 快照；
- 提交形态择一：抬水位同事务写窗口证明，缺一整批回滚（失败批不留窗口残骸）；
- 硬约束：成员占用均落在 (from, to] 内，越界即整批回滚；
- 只读核证：latest_window() 末趟窗口、list_windows()/window_for_batch() 每趟可查、
  window_proof() 窗口链与水位互证 + 三方对账。
"""

STATUS = "implemented"

from app.services.reclaim import (  # noqa: E402,F401
    latest_window,
    list_windows,
    window_for_batch,
    window_proof,
)
