import asyncio
import logging
import os
import random
import time
from collections import defaultdict
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging.exceptions import ApiException
from linebot.v3.webhooks import (
    FollowEvent, ImageMessageContent, JoinEvent, LeaveEvent, MessageEvent,
    StickerMessageContent, TextMessageContent, UnfollowEvent,
)

load_dotenv()

import config  # noqa: E402
from bot import clock  # noqa: E402
from bot.db import DB  # noqa: E402
from bot.graph import Companion, thread_id  # noqa: E402
from bot.line_io import LineIO, is_mentioned, split_bubbles, strip_mentions  # noqa: E402
from bot.proactive import Proactive  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

parser = WebhookParser(os.getenv("CHANNEL_SECRET"))
chat_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # 同一個對話一次只處理一則
deps: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = DB()
    line = LineIO()
    companion = Companion(db)
    proactive = Proactive(db, companion, send=line.push, quota_left=line.quota_left)
    scheduler = AsyncIOScheduler(timezone=config.TZ)
    scheduler.add_job(proactive.tick, "interval", minutes=config.TICK_MINUTES,
                      next_run_time=clock.now(), max_instances=1, coalesce=True)
    scheduler.start()
    deps.update(db=db, line=line, companion=companion)
    log.info("啟動完成")
    yield
    scheduler.shutdown(wait=False)
    await line.close()


app = FastAPI(lifespan=lifespan)


@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        raise HTTPException(400, "missing signature")
    body = (await request.body()).decode()
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        raise HTTPException(400, "invalid signature")
    for event in events:
        background_tasks.add_task(handle_event, event)
    return "OK"


async def handle_event(event):
    try:
        if isinstance(event, MessageEvent):
            await handle_message(event)
        elif isinstance(event, FollowEvent):
            await handle_follow(event)
        elif isinstance(event, UnfollowEvent):
            deps["db"].update_user(event.source.user_id, is_friend=0)
            deps["db"].execute("UPDATE proactive_schedule SET status='skipped' WHERE user_id=? AND status='pending'",
                               (event.source.user_id,))
        elif isinstance(event, JoinEvent):
            deps["db"].set_group(_chat_of(event.source)[1], True)
        elif isinstance(event, LeaveEvent):
            deps["db"].set_group(_chat_of(event.source)[1], False)
    except Exception:
        log.exception("handle_event failed")


def _chat_of(source) -> tuple[str, str]:
    if source.type == "group":
        return "group", source.group_id
    if source.type == "room":
        return "group", source.room_id
    return "user", source.user_id


def _message_text(message, chat_type: str) -> tuple[str | None, bool]:
    """回傳 (文字, 是否在跟她說話)。一對一一定是在跟她說話；群組要 @ 她才算。"""
    is_user = chat_type == "user"
    if isinstance(message, TextMessageContent):
        if is_user:
            return message.text, True
        mentioned = is_mentioned(message)
        return (strip_mentions(message) if mentioned else message.text), mentioned
    if isinstance(message, StickerMessageContent):
        return "（傳了一張貼圖）", is_user
    if isinstance(message, ImageMessageContent):
        return "（傳了一張照片）", is_user
    return None, False


async def _speaker_name(user_id: str | None, group_id: str) -> str:
    """群組發話者名稱，查過一次就存在 users.display_name。"""
    db = deps["db"]
    if not user_id:
        return "某人"
    user = db.get_user(user_id)
    if not user["display_name"]:
        name = await deps["line"].display_name(user_id, group_id)
        db.update_user(user_id, display_name=name)
        return name or "某人"
    return user["display_name"]


async def record_group_context(chat_id: str, user_id: str | None, text: str):
    """群組裡沒 @ 她的訊息：只當群組上下文，不進個人記憶、不影響好感度。"""
    companion = deps["companion"]
    line_text = f"[{await _speaker_name(user_id, chat_id)}] {text}"
    async with chat_locks[thread_id("group", chat_id)]:
        await asyncio.to_thread(companion.append_context, chat_id, line_text)
        await asyncio.to_thread(companion.summarize_if_needed, "group", chat_id)


async def handle_message(event: MessageEvent):
    db, line, companion = deps["db"], deps["line"], deps["companion"]
    chat_type, chat_id = _chat_of(event.source)
    user_id = getattr(event.source, "user_id", None)
    text, addressed = _message_text(event.message, chat_type)
    if text is None:  # 不支援的訊息類型
        return
    if not addressed:  # 群組裡沒 @ 她
        if config.GROUP_CONTEXT_ENABLED:
            await record_group_context(chat_id, user_id, text)
        return
    started = time.monotonic()

    if user_id:
        db.get_user(user_id)
        if chat_type == "user":
            db.update_user(user_id, is_friend=1)
        db.record_user_message(user_id, clock.now())

    image = None
    if chat_type == "group":
        text = f"[{await _speaker_name(user_id, chat_id)}] {text}"
    else:
        if config.IMAGE_ENABLED and isinstance(event.message, ImageMessageContent):
            image = await line.get_image(event.message.id)
        token = getattr(event.message, "mark_as_read_token", None)
        if config.MARK_AS_READ and token:
            await asyncio.sleep(random.uniform(*config.READ_DELAY_SEC))
            await line.mark_read(token)
        await line.loading(user_id)

    lock = chat_locks[thread_id(chat_type, chat_id)]
    async with lock:
        reply = await asyncio.to_thread(companion.reply, chat_type=chat_type, chat_id=chat_id,
                                        speaker_id=user_id, text=text, image=image)
    await send_reply(event.reply_token, chat_id, split_bubbles(reply), time.monotonic() - started)

    if image:  # 把 thread 裡的原圖換成文字描述，記憶抽取也用這段描述
        async with lock:
            text = await asyncio.to_thread(companion.compact_image, chat_type, chat_id) or text
    await asyncio.to_thread(companion.post_process, chat_type=chat_type, chat_id=chat_id,
                            speaker_id=user_id, user_text=text, ai_text=reply)
    async with lock:
        await asyncio.to_thread(companion.summarize_if_needed, chat_type, chat_id)


async def send_reply(reply_token: str, chat_id: str, bubbles: list[str], elapsed: float):
    """優先用免費的 reply；token 快過期或失效時改用 push 補送（會消耗額度）。"""
    db, line = deps["db"], deps["line"]
    if elapsed < config.REPLY_TOKEN_SAFE_SEC:
        try:
            await line.reply(reply_token, bubbles)
            return
        except ApiException as e:
            log.warning("reply failed (%s), fallback to push", e.status)
    try:
        await line.push(chat_id, bubbles)
        db.log_push(chat_id, clock.now())
    except ApiException:
        log.exception("push failed")


async def handle_follow(event: FollowEvent):
    db, line, companion = deps["db"], deps["line"], deps["companion"]
    user_id = event.source.user_id
    trigger = "welcome_back" if db.get_user(user_id)["user_msg_count"] else "welcome"
    db.update_user(user_id, is_friend=1, display_name=await line.display_name(user_id))
    async with chat_locks[thread_id("user", user_id)]:
        welcome = await asyncio.to_thread(companion.proactive, user_id=user_id, trigger=trigger)
    await send_reply(event.reply_token, user_id, split_bubbles(welcome), 0)


if __name__ == "__main__":  # 一鍵啟動伺服器
    uvicorn.run("server:app", port=config.PORT, reload=True)
