"""主動傳訊：每天排程（plan_day）＋ 每 N 分鐘檢查到點的排程（tick）。

例：00:05 排出「某人｜14:23｜等待中」→ 14:20 的 tick 還沒到點 → 14:25 的 tick 發現到點，
檢查條件，通過就產生訊息並 push，不通過就跳過。
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Awaitable, Callable

import config
from bot import affection, clock, memory
from bot.db import DB
from bot.graph import Companion
from bot.line_io import split_bubbles

log = logging.getLogger(__name__)

SendFn = Callable[[str, list[str]], Awaitable[None]]
QuotaFn = Callable[[], Awaitable[int | None]]


class Proactive:
    def __init__(self, db: DB, companion: Companion, send: SendFn, quota_left: QuotaFn | None = None):
        self.db = db
        self.companion = companion
        self.send = send
        self.quota_left = quota_left
        self.lock = asyncio.Lock()

    # ---------------- 每日排程 ----------------
    def plan_day(self, now: datetime, force: bool = False) -> list[dict]:
        today = now.date().isoformat()
        if not force and self.db.get_meta("planned_date") == today:
            return []
        affection.daily_decay(self.db, now)
        self.db.execute("UPDATE proactive_schedule SET status='expired' WHERE status='pending' AND plan_date<?",
                        (today,))

        rows = []
        for user in self.db.friends():
            uid = user["user_id"]
            # 事件觸發：生日、記憶中當天的事件（不受好感度與訊息數限制）
            if memory.get_profile(self.companion.store, uid).get("birthday") == now.strftime("%m-%d"):
                rows.append((uid, clock.at(now, config.BIRTHDAY_SEND_AT), "birthday", None))
            for m in memory.memories_on(self.companion.store, uid, today):
                rows.append((uid, self._random_time(now, *config.EVENT_SEND_WINDOW), "event", m["text"]))

            # 想念：依好感度階段的每日期望則數
            stage = affection.stage_of(user["affection"])
            expected = config.PROACTIVE_DAILY_EXPECTED.get(stage, 0)
            if user["user_msg_count"] < config.PROACTIVE_MIN_USER_MSGS or expected <= 0:
                continue
            count = int(expected) + (random.random() < expected - int(expected))
            for _ in range(min(count, config.PROACTIVE_MAX_PER_DAY)):
                rows.append((uid, self._random_time(now, config.QUIET_END, config.QUIET_START), "miss", None))

        planned = []
        for uid, at, trigger, detail in rows:
            if at is None or at < now:
                continue
            self.db.execute(
                "INSERT INTO proactive_schedule (user_id, plan_date, scheduled_at, trigger, detail) VALUES (?,?,?,?,?)",
                (uid, today, at.isoformat(), trigger, detail))
            planned.append({"user_id": uid, "at": f"{at:%H:%M}", "trigger": trigger, "detail": detail})
        self.db.set_meta("planned_date", today)
        log.info("plan_day %s: %s", today, planned)
        return planned

    @staticmethod
    def _random_time(now: datetime, start_hm: str, end_hm: str) -> datetime | None:
        start, end = clock.at(now, start_hm), clock.at(now, end_hm)
        if end <= start:
            end = clock.at(now, "23:59")
        start = max(start, now + timedelta(minutes=1))
        if start >= end:
            return None
        return start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))

    # ---------------- 定時檢查 ----------------
    async def tick(self) -> list[dict]:
        async with self.lock:
            return await self._tick(clock.now())

    async def _tick(self, now: datetime) -> list[dict]:
        if not config.PROACTIVE_ENABLED:
            return []
        if self.db.get_meta("planned_date") != now.date().isoformat() and now >= clock.at(now, config.PLAN_DAY_AT):
            self.plan_day(now)
        self._apply_ignore_penalty(now)

        due = self.db.all("SELECT * FROM proactive_schedule WHERE status='pending' AND scheduled_at<=? "
                          "ORDER BY scheduled_at", (now.isoformat(),))
        results = []
        for row in due:
            reason = await self._skip_reason(row, now)
            if reason:
                status = "expired" if reason == "過期" else "skipped"
                self.db.execute("UPDATE proactive_schedule SET status=? WHERE id=?", (status, row["id"]))
                results.append({**row, "status": status, "reason": reason})
                log.info("proactive skip %s %s: %s", row["user_id"], row["trigger"], reason)
                continue
            text = await asyncio.to_thread(self.companion.proactive, user_id=row["user_id"],
                                           trigger=row["trigger"], detail=row["detail"])
            await self.send(row["user_id"], split_bubbles(text))
            self.db.log_push(row["user_id"], now)
            self.db.update_user(row["user_id"], last_proactive_at=now.isoformat(), proactive_state="waiting")
            self.db.execute("UPDATE proactive_schedule SET status='sent' WHERE id=?", (row["id"],))
            results.append({**row, "status": "sent", "text": text})
            log.info("proactive sent %s %s: %s", row["user_id"], row["trigger"], text)
        return results

    async def _skip_reason(self, row: dict, now: datetime) -> str | None:
        if datetime.fromisoformat(row["scheduled_at"]) < now - timedelta(minutes=config.SCHEDULE_EXPIRE_MINUTES):
            return "過期"
        if clock.in_quiet_hours(now):
            return "安靜時段"
        user = self.db.get_user(row["user_id"])
        if not user["is_friend"]:
            return "已不是好友"
        if self.db.pushes_this_month(now) >= config.MONTHLY_PUSH_BUDGET:
            return "本月推播預算用完"
        if self.quota_left and (left := await self.quota_left()) is not None and left <= 0:
            return "LINE 額度用完"
        if row["trigger"] == "miss":
            last = user["last_user_msg_at"]
            if last and now - datetime.fromisoformat(last) < timedelta(hours=config.MIN_IDLE_HOURS):
                return "剛聊過"
            if user["proactive_state"] == "waiting":
                return "上一則主動訊息還沒回"
            sent = user["last_proactive_at"]
            if sent and now - datetime.fromisoformat(sent) < timedelta(hours=config.MIN_GAP_HOURS):
                return "離上一則主動訊息太近"
        return None

    def _apply_ignore_penalty(self, now: datetime):
        cutoff = (now - timedelta(hours=config.IGNORE_HOURS)).isoformat()
        for user in self.db.all("SELECT user_id FROM users WHERE proactive_state='waiting' AND last_proactive_at<?",
                                (cutoff,)):
            affection.ignore_penalty(self.db, user["user_id"])
            log.info("ignore penalty %s", user["user_id"])
