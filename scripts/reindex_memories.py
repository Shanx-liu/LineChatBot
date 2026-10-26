"""重新計算所有事件記憶的向量（內容不變）。

什麼時候要跑：
- 換了 EMBED_MODEL 之後
- 2026-10 之前的版本把整筆 JSON 拿去算向量（設定欄位寫錯），升級後跑一次，讓語意搜尋與去重恢復正常

執行前會先把 graph.sqlite 備份成 graph.sqlite.bak-時間。伺服器執行中也可以跑。
用法：python scripts/reindex_memories.py
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import config  # noqa: E402
from bot.db import DB  # noqa: E402
from bot.graph import Companion  # noqa: E402


def main():
    src = config.GRAPH_DB_PATH
    if not src.exists():
        print("找不到", src)
        return
    backup = src.with_name(f"{src.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    with sqlite3.connect(src) as conn, sqlite3.connect(backup) as dst:
        conn.backup(dst)  # 線上備份，伺服器執行中也安全
    print("已備份到", backup)

    companion = Companion(DB())
    prefixes = [r[0] for r in sqlite3.connect(src).execute(
        "SELECT DISTINCT prefix FROM store WHERE prefix LIKE 'user.%.memories'")]
    total = 0
    for prefix in prefixes:
        ns = tuple(prefix.split("."))
        items = companion.store.search(ns, limit=10000)
        for item in items:
            companion.store.put(ns, item.key, item.value)  # 同 key 重寫 → 重新計算向量
        total += len(items)
        print(f"  {prefix}: {len(items)} 筆")
    print(f"完成，共重建 {total} 筆記憶")


if __name__ == "__main__":
    main()
