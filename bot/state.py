from typing import Literal, Optional

from langgraph.graph import MessagesState


class ChatState(MessagesState):
    mode: Literal["reply", "proactive"]
    chat_type: Literal["user", "group"]
    chat_id: str                    # 一對一 = userId，群組 = groupId
    speaker_id: Optional[str]       # 發話者 userId（群組裡用來對應個人記憶；可能拿不到）
    summary: str                    # 較早對話的滾動摘要
    profile: dict                   # 長期記憶：個人資料
    memories: list[dict]            # 長期記憶：本輪相關的事件記憶
    affection: int
    trigger: Optional[str]          # 主動訊息觸發原因：miss / birthday / event / welcome
    trigger_detail: Optional[str]
