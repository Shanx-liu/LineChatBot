"""LINE Messaging API（v3 SDK）包裝。"""
import logging
import os
import random

from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, AsyncMessagingApiBlob, Configuration, MarkMessagesAsReadByTokenRequest,
    PushMessageRequest, ReplyMessageRequest, ShowLoadingAnimationRequest, TextMessage,
)

import config

log = logging.getLogger(__name__)


def split_bubbles(text: str) -> list[str]:
    bubbles = [b.strip() for b in text.split("|||") if b.strip()] or [text.strip() or "……"]
    if len(bubbles) > 1 and random.random() >= config.TWO_BUBBLE_RATE:
        return [" ".join(bubbles)]  # 大部分情況合併成 1 個泡泡
    if len(bubbles) > config.MAX_BUBBLES:
        head = bubbles[:config.MAX_BUBBLES - 1]
        bubbles = head + ["\n".join(bubbles[config.MAX_BUBBLES - 1:])]
    return bubbles


def is_mentioned(message) -> bool:
    mention = getattr(message, "mention", None)
    return bool(mention and any(getattr(m, "is_self", False) for m in mention.mentionees))


def strip_mentions(message) -> str:
    """把 @某人 從訊息文字中拿掉（從後往前刪，避免 index 位移）。"""
    text = message.text
    mention = getattr(message, "mention", None)
    if mention:
        for m in sorted(mention.mentionees, key=lambda m: m.index, reverse=True):
            text = text[:m.index] + text[m.index + m.length:]
    return text.strip()


class LineIO:
    def __init__(self):
        cfg = Configuration(access_token=os.getenv("CHANNEL_ACCESS_TOKEN"))
        self.client = AsyncApiClient(cfg)
        self.api = AsyncMessagingApi(self.client)
        self.blob = AsyncMessagingApiBlob(self.client)

    async def get_image(self, message_id: str) -> bytes | None:
        """下載使用者傳的圖片（LINE 只保留一段時間，要在收到後盡快下載）。"""
        try:
            return bytes(await self.blob.get_message_content(message_id))
        except Exception as e:
            log.warning("get_image failed: %s", e)
            return None

    async def close(self):
        await self.client.close()

    @staticmethod
    def _messages(texts: list[str]):
        return [TextMessage(text=t) for t in texts]

    async def reply(self, reply_token: str, texts: list[str]):
        await self.api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=self._messages(texts)))

    async def push(self, to: str, texts: list[str]):
        await self.api.push_message(PushMessageRequest(to=to, messages=self._messages(texts)))

    async def mark_read(self, token: str):
        try:
            await self.api.mark_messages_as_read_by_token(MarkMessagesAsReadByTokenRequest(mark_as_read_token=token))
        except Exception as e:  # 聊天功能關閉時會自動已讀，這裡失敗不影響回覆
            log.warning("mark_read failed: %s", e)

    async def loading(self, user_id: str, seconds: int = config.LOADING_SECONDS):
        try:
            await self.api.show_loading_animation(
                ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=seconds))
        except Exception as e:
            log.warning("loading animation failed: %s", e)

    async def display_name(self, user_id: str, group_id: str | None = None) -> str | None:
        try:
            if group_id:
                return (await self.api.get_group_member_profile(group_id, user_id)).display_name
            return (await self.api.get_profile(user_id)).display_name
        except Exception as e:
            log.warning("get profile failed: %s", e)
            return None

    async def quota_left(self) -> int | None:
        """本月剩餘可推播則數；無上限（付費方案）時回傳 None。"""
        try:
            quota = await self.api.get_message_quota()
            if quota.type != "limited":
                return None
            used = await self.api.get_message_quota_consumption()
            return quota.value - used.total_usage
        except Exception as e:
            log.warning("quota check failed: %s", e)
            return None
