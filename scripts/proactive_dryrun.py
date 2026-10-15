"""主動傳訊演練：不真的 push，只把排程與產生的訊息印出來。

用法：python scripts/proactive_dryrun.py
會建立一個測試使用者（好感度 80、生日是今天、今天有面試），然後：
  1. 執行 plan_day 印出今天的排程
  2. 把時鐘快轉到每個排程時間並執行 tick，印出結果
  3. 測試條件：剛聊過 → 跳過；上一則沒回 → 跳過
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import config  # noqa: E402
from bot import clock, memory  # noqa: E402
from bot.db import DB  # noqa: E402
from bot.graph import Companion  # noqa: E402
from bot.proactive import Proactive  # noqa: E402

USER = "dryrun-user"


async def fake_send(user_id, texts):
    print(f"  [PUSH → {user_id}]")
    for t in texts:
        print(f"    姐姐 > {t}")


async def main():
    for p in config.DATA_DIR.glob("dryrun_*.sqlite*"):
        p.unlink()
    db = DB(config.DATA_DIR / "dryrun_bot.sqlite")
    companion = Companion(db, config.DATA_DIR / "dryrun_graph.sqlite")
    pro = Proactive(db, companion, fake_send)

    # 時鐘先撥到今天 08:00，讓整天都排得進去
    now = clock.now()
    clock.shift(seconds=(clock.at(now, "08:00") - now).total_seconds())
    now = clock.now()

    db.get_user(USER)
    db.update_user(USER, is_friend=1, affection=80, user_msg_count=50,
                   last_user_msg_at=(now - timedelta(days=1)).isoformat())
    memory.update_profile(companion.store, USER, {"name": "阿哲", "birthday": now.strftime("%m-%d"), "likes": ["拉麵"]})
    memory.add_memories(companion.store, USER, [{"text": "他今天下午要去面試", "date": now.date().isoformat()}],
                        "private", now)

    print(f"== plan_day（{now:%Y-%m-%d %H:%M}，好感度 80／曖昧）")
    for p in pro.plan_day(now, force=True):
        print("  ", p)

    rows = db.all("SELECT * FROM proactive_schedule WHERE status='pending' ORDER BY scheduled_at")
    for row in rows:
        target = clock.at(clock.now(), row["scheduled_at"][11:16]) + timedelta(minutes=config.TICK_MINUTES)
        clock.shift(seconds=max(0, (target - clock.now()).total_seconds()))
        print(f"\n== tick @ {clock.now():%H:%M}（排程 {row['scheduled_at'][11:16]} {row['trigger']}）")
        for r in await pro.tick():
            if r["status"] != "sent":
                print(f"  跳過：{r['reason']}")
        db.update_user(USER, proactive_state="replied")  # 假裝他回了

    print("\n== 條件測試：剛聊過 → 應跳過")
    db.update_user(USER, last_user_msg_at=clock.now().isoformat())
    db.execute("INSERT INTO proactive_schedule (user_id, plan_date, scheduled_at, trigger) VALUES (?,?,?,?)",
               (USER, clock.now().date().isoformat(), clock.now().isoformat(), "miss"))
    for r in await pro.tick():
        print(f"  {r['status']}：{r.get('reason')}")

    print("\n== 條件測試：上一則主動訊息沒回 → 應跳過")
    db.update_user(USER, last_user_msg_at=(clock.now() - timedelta(hours=5)).isoformat(), proactive_state="waiting",
                   last_proactive_at=clock.now().isoformat())
    db.execute("INSERT INTO proactive_schedule (user_id, plan_date, scheduled_at, trigger) VALUES (?,?,?,?)",
               (USER, clock.now().date().isoformat(), clock.now().isoformat(), "miss"))
    for r in await pro.tick():
        print(f"  {r['status']}：{r.get('reason')}")

    print(f"\n本月推播計數：{db.pushes_this_month(clock.now())}")


if __name__ == "__main__":
    asyncio.run(main())
