"""回收扫描水位（功能题实现）。

实现位于 app.services.reclaim：
- 持久水位形态择一：独立 reclaim_scan_watermarks 表（按 scanner_name 记录），非内存、非 settings 键；
- 失败闭路径择一：本批关账与水位更新同一事务（BEGIN IMMEDIATE ... commit/rollback）；
- 只读核证：watermark_proof() 给水位、本批关闭数、成员占用。
"""

STATUS = "implemented"

from app.services.reclaim import (  # noqa: E402,F401
    DEFAULT_SCANNER,
    get_watermark,
    run_scan,
    watermark_proof,
)
