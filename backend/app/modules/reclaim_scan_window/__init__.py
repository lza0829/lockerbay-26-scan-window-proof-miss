
STATUS = "partial"


def latest_window(conn) -> dict:
    wm = conn.execute(
        "SELECT value FROM settings WHERE key='reclaim_watermark'"
    ).fetchone()
    return {
        "from_watermark": None,
        "to_watermark": wm["value"] if wm else None,
        "member_ids": [],
        "complete": False,
    }
