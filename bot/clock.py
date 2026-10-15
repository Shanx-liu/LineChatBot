"""統一取得現在時間。測試時可以用 shift() 快轉，驗證好感度門檻、主動排程等跨日邏輯。"""
from datetime import datetime, timedelta

import config

_offset = timedelta()


def now() -> datetime:
    return datetime.now(config.TZ) + _offset


def shift(**kwargs) -> None:
    global _offset
    _offset += timedelta(**kwargs)


def parse_hm(hm: str) -> tuple[int, int]:
    h, m = hm.split(":")
    return int(h), int(m)


def at(day: datetime, hm: str) -> datetime:
    h, m = parse_hm(hm)
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def in_quiet_hours(t: datetime) -> bool:
    start, end = at(t, config.QUIET_START), at(t, config.QUIET_END)
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # 跨午夜
