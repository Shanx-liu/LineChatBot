"""長期記憶：個人資料（profile）＋ 事件記憶（向量搜尋）。

namespace：
  ("user", user_id)              key="profile"  → 個人資料，一對一和所有群組共用
  ("user", user_id, "memories")  key=uuid       → 事件記憶，value 帶 source（private / group:{id}）
"""
import uuid
from datetime import datetime

from langgraph.store.base import BaseStore

LIST_FIELDS = ("likes", "dislikes", "facts")
SCALAR_FIELDS = ("name", "nickname", "birthday")
MAX_LIST = 30
DUPLICATE_SCORE = 0.9


def _profile_ns(user_id):
    return ("user", user_id)


def _memory_ns(user_id):
    return ("user", user_id, "memories")


def get_profile(store: BaseStore, user_id: str) -> dict:
    item = store.get(_profile_ns(user_id), "profile")
    return item.value if item else {}


def update_profile(store: BaseStore, user_id: str, updates: dict) -> dict:
    profile = get_profile(store, user_id)
    for key in SCALAR_FIELDS:
        if updates.get(key):
            profile[key] = updates[key]
    for key in LIST_FIELDS:
        new_items = [x for x in updates.get(key) or [] if x]
        if new_items:
            merged = profile.get(key, []) + [x for x in new_items if x not in profile.get(key, [])]
            profile[key] = merged[-MAX_LIST:]
    store.put(_profile_ns(user_id), "profile", profile, index=False)
    return profile


def add_memories(store: BaseStore, user_id: str, items: list[dict], source: str, now: datetime) -> int:
    """items: [{"text": ..., "date": "YYYY-MM-DD" | None}]。和既有記憶太像的會略過。"""
    added = 0
    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        similar = store.search(_memory_ns(user_id), query=text, limit=1)
        if similar and similar[0].score is not None and similar[0].score >= DUPLICATE_SCORE:
            continue
        value = {"text": text, "source": source, "created_at": now.date().isoformat()}
        if item.get("date"):
            value["date"] = item["date"]
        store.put(_memory_ns(user_id), uuid.uuid4().hex, value)
        added += 1
    return added


def search_memories(store: BaseStore, user_id: str, query: str, k: int, group_id: str | None = None) -> list[dict]:
    """私聊可用全部記憶；群組只用來自同一個群組的記憶（隱私規則）。"""
    flt = {"source": f"group:{group_id}"} if group_id else None
    return [r.value for r in store.search(_memory_ns(user_id), query=query or None, filter=flt, limit=k)]


def memories_on(store: BaseStore, user_id: str, day: str) -> list[dict]:
    """取出日期是 day（YYYY-MM-DD）的事件記憶，用來排事件追蹤的主動訊息。"""
    return [r.value for r in store.search(_memory_ns(user_id), filter={"date": day}, limit=10)]


def all_memories(store: BaseStore, user_id: str, limit: int = 100) -> list[dict]:
    return [r.value for r in store.search(_memory_ns(user_id), limit=limit)]
