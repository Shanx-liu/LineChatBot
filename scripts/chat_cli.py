"""在終端機直接跟她聊天（不經過 LINE），用來測試記憶、好感度、摘要、主動訊息。

用法：python scripts/chat_cli.py [--reset]
指令：
  /aff               看好感度
  /set 75            直接設定好感度
  /profile  /mem     看個人資料／事件記憶
  /summary           看目前摘要與 thread 訊息數
  /day 3             時鐘快轉 3 天（測門檻、衰減）
  /proactive [miss|birthday|event|welcome] [補充說明]
  /quit
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import config  # noqa: E402
from bot import affection, clock, memory  # noqa: E402
from bot.db import DB  # noqa: E402
from bot.graph import Companion  # noqa: E402

USER = "cli-user"
TEST_DB = config.DATA_DIR / "test_bot.sqlite"
TEST_GRAPH_DB = config.DATA_DIR / "test_graph.sqlite"


def show(text: str):
    for bubble in [b.strip() for b in text.split("|||") if b.strip()]:
        print(f"  姐姐 > {bubble}")


def status(db: DB):
    u = db.get_user(USER)
    print(f"  [好感度 {u['affection']}｜{affection.stage_of(u['affection'])}｜今日已加 {u['gain_today']}"
          f"｜互動天數 {u['active_days']}｜訊息數 {u['user_msg_count']}]")


def main():
    if "--reset" in sys.argv:
        for p in config.DATA_DIR.glob("test_*.sqlite*"):
            p.unlink()
    logging.basicConfig(level=logging.WARNING)
    db = DB(TEST_DB)
    bot = Companion(db, TEST_GRAPH_DB)
    db.get_user(USER)
    db.update_user(USER, is_friend=1)
    print("開始聊天（/quit 離開）")

    while True:
        try:
            text = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.startswith("/"):
            cmd, *args = text.split(maxsplit=2)
            if cmd == "/quit":
                break
            elif cmd == "/aff":
                status(db)
            elif cmd == "/set":
                db.update_user(USER, affection=int(args[0]))
                status(db)
            elif cmd == "/profile":
                print(" ", memory.get_profile(bot.store, USER))
            elif cmd == "/mem":
                for m in memory.all_memories(bot.store, USER):
                    print("  -", m)
            elif cmd == "/summary":
                values = bot.history("user", USER)
                print(f"  訊息數 {len(values.get('messages', []))}｜摘要：{values.get('summary') or '（無）'}")
            elif cmd == "/day":
                clock.shift(days=int(args[0]))
                affection.daily_decay(db, clock.now())
                print(f"  現在時間：{clock.now():%Y-%m-%d %H:%M}")
                status(db)
            elif cmd == "/proactive":
                trigger = args[0] if args else "miss"
                show(bot.proactive(user_id=USER, trigger=trigger, detail=args[1] if len(args) > 1 else None))
            else:
                print("  未知指令")
            continue

        db.record_user_message(USER, clock.now())
        reply = bot.reply(chat_type="user", chat_id=USER, speaker_id=USER, text=text)
        show(reply)
        result = bot.after_reply(chat_type="user", chat_id=USER, speaker_id=USER, user_text=text, ai_text=reply)
        if result:
            extra = f"｜記住 {result['memories_added']} 件" if result["memories_added"] else ""
            prof = f"｜資料 {result['profile']}" if result["profile"] else ""
            print(f"  [評分 {result['delta']:+d}→實際 {result['applied']:+d}｜{result['reason']}{prof}{extra}]")
            status(db)


if __name__ == "__main__":
    main()
