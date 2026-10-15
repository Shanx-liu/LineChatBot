"""好感度：階段判定、每日上限、門檻、衰減。"""
from datetime import date, datetime

import config
from bot.db import DB

STAGE_NAMES = [name for name, _, _ in config.AFFECTION_STAGES]


def stage_of(score: int) -> str:
    for name, low, high in config.AFFECTION_STAGES:
        if low <= score <= high:
            return name
    return STAGE_NAMES[-1] if score > 100 else STAGE_NAMES[0]


def stage_index(name: str) -> int:
    return STAGE_NAMES.index(name)


def _stage_low(name: str) -> int:
    return next(low for n, low, _ in config.AFFECTION_STAGES if n == name)


def _gate_met(user: dict, stage: str, now: datetime) -> bool:
    gate = config.STAGE_GATES.get(stage)
    if not gate:
        return True
    days_known = (now - datetime.fromisoformat(user["first_seen"])).days
    return days_known >= gate["days_known"] and user["active_days"] >= gate["active_days"]


def apply_delta(db: DB, user_id: str, delta: int, now: datetime) -> tuple[int, int]:
    """套用一次評分。回傳 (新分數, 實際變化量)。"""
    user = db.get_user(user_id)
    score = user["affection"]
    delta = max(-config.DELTA_PER_MSG, min(config.DELTA_PER_MSG, delta))
    fields = {}

    if delta > 0:
        today = now.date().isoformat()
        gain_today = user["gain_today"] if user["gain_date"] == today else 0
        allowed = max(0, config.DAILY_GAIN_CAP[stage_of(score)] - gain_today)
        new = min(100, score + min(delta, allowed))
        # 門檻沒達到時，卡在前一個階段的上限
        for stage in config.STAGE_GATES:
            if stage_index(stage_of(new)) >= stage_index(stage) and not _gate_met(user, stage, now):
                new = min(new, _stage_low(stage) - 1)
        new = max(new, score)
        fields.update(gain_date=today, gain_today=gain_today + (new - score))
    else:
        new = max(0, score + delta)

    fields["affection"] = new
    db.update_user(user_id, **fields)
    return new, new - score


def daily_decay(db: DB, now: datetime) -> None:
    """連續 N 天沒互動後每天 -1，不低於目前階段的下限。每天執行一次。"""
    today = now.date()
    for user in db.all("SELECT * FROM users WHERE last_active_date IS NOT NULL"):
        if user["last_decay_date"] == today.isoformat():
            continue
        idle_days = (today - date.fromisoformat(user["last_active_date"])).days
        if idle_days >= config.INACTIVE_DECAY_AFTER_DAYS:
            score = user["affection"]
            floor = _stage_low(stage_of(score))
            db.update_user(user["user_id"], affection=max(floor, score - 1),
                           last_decay_date=today.isoformat())


def ignore_penalty(db: DB, user_id: str) -> None:
    user = db.get_user(user_id)
    db.update_user(user_id, affection=max(0, user["affection"] - 1), proactive_state="ignored")
