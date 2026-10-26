"""組裝 system prompt：人設 + 好感度階段 + 記憶 + 範例 + 句庫 + 時間。"""
import json
import random
import re
from datetime import datetime

import config
from bot.affection import STAGE_NAMES, stage_index, stage_of


def _read(name: str) -> str:
    return (config.PERSONA_DIR / name).read_text(encoding="utf-8")


CORE = _read("core.md")
LINE_STYLE = _read("line_style.md")
GROUP_STYLE = _read("group_style.md")
PROACTIVE = _read("proactive.md")
EXAMPLES = json.loads(_read("examples.json"))["examples"]
QUOTES = json.loads(_read("quotes.json"))["quotes"]


def _stage_sections(name: str) -> dict[str, str]:
    """解析以「## 階段名稱」分段的檔案 → {階段名稱: 段落內容}。"""
    return {
        m.group(1).strip(): m.group(2).strip()
        for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", _read(name), re.M | re.S)
    }


STAGE_TEXT = _stage_sections("affection_levels.md")
BACKSTORY = _stage_sections("backstory.md")

BACKSTORY_RULES = """以下是你自己的經歷與日常，是你真實的記憶。
- 他問到或話題相關時，才自然地分享，一次只說一點，不要一口氣把故事全說完。
- 越私密的事越要看氣氛，可以先欲言又止，等他追問再說。
- 他問到這裡沒寫的、更私人的事，就俏皮地帶過（「這個嘛 等你更了解姐姐再說」），不要編造。"""


def backstory_text(stage: str, is_group: bool) -> str:
    """依目前階段累積放入故事段落；群組只放公開的部分。"""
    upto = 0 if is_group else stage_index(stage)
    sections = [BACKSTORY[name] for name in STAGE_NAMES[:upto + 1] if name in BACKSTORY]
    return "\n\n".join(sections)

SCENARIO_KEYWORDS = {
    "flirt": ["想你", "抱抱", "喜歡你", "愛你", "漂亮", "親親", "好美", "約會", "想見你", "吃醋"],
    "low": ["難過", "傷心", "心情不好", "委屈", "想哭", "沒用", "失望", "崩潰", "不開心", "好煩", "沒意思"],
    "tired": ["好累", "很累", "疲憊", "加班", "好忙", "沒力", "累死"],
    "lost": ["不知道", "怎麼辦", "該選", "迷茫", "失敗", "做不到", "猶豫"],
    "hurt": ["受傷", "好痛", "生病", "感冒", "發燒", "不舒服"],
    "lonely": ["孤單", "孤獨", "一個人", "寂寞"],
    "scared": ["害怕", "好怕", "恐怖"],
    "night": ["晚安", "睡不著", "失眠", "要睡了"],
    "farewell": ["掰掰", "拜拜", "先走", "要走了", "去忙"],
}

TRIGGER_TEXT = {
    "miss": "想念（隨機想到他）",
    "birthday": "生日（今天是他的生日）",
    "event": "事件追蹤",
    "welcome": "初次見面",
    "welcome_back": "他封鎖你之後又把你加回好友了（可以帶點小埋怨，但歡迎他回來）",
}

WEEKDAYS = "一二三四五六日"


def detect_scenario(text: str) -> str:
    for scenario, words in SCENARIO_KEYWORDS.items():
        if any(w in text for w in words):
            return scenario
    return "daily"


def _allowed(item: dict, stage: str) -> bool:
    return item.get("min_stage") is None or stage_index(stage) >= stage_index(item["min_stage"])


def pick_examples(scenario: str, stage: str, n: int = config.FEW_SHOT_COUNT) -> list[dict]:
    pool = [e for e in EXAMPLES if _allowed(e, stage)]
    same = [e for e in pool if e["scenario"] == scenario]
    rest = [e for e in pool if e["scenario"] != scenario]
    random.shuffle(same)
    random.shuffle(rest)
    return (same + rest)[:n]


def pick_quotes(scenario: str, stage: str, n: int = config.QUOTE_COUNT) -> list[str]:
    pool = [q for q in QUOTES if _allowed(q, stage)]
    same = [q for q in pool if scenario in q["scenarios"]] or pool
    return [q["text"] for q in random.sample(same, min(n, len(same)))]


def time_text(now: datetime) -> str:
    h = now.hour
    period = ("深夜" if h < 5 else "清晨" if h < 8 else "早上" if h < 11 else "中午" if h < 13
              else "下午" if h < 17 else "傍晚" if h < 19 else "晚上" if h < 23 else "深夜")
    return f"{now:%Y-%m-%d} 星期{WEEKDAYS[now.weekday()]} {now:%H:%M}（{period}）"


def profile_text(profile: dict) -> str:
    labels = {"name": "名字", "nickname": "暱稱", "birthday": "生日", "likes": "喜歡",
              "dislikes": "不喜歡", "facts": "其他"}
    lines = []
    for key, label in labels.items():
        value = profile.get(key)
        if value:
            lines.append(f"- {label}：{'、'.join(value) if isinstance(value, list) else value}")
    return "\n".join(lines) or "（還不太了解他）"


def memories_text(memories: list[dict]) -> str:
    if not memories:
        return "（沒有相關記憶）"
    return "\n".join(f"- {m['text']}（{m.get('date') or m.get('created_at', '')}）" for m in memories)


def build_system_prompt(state: dict, now: datetime, user_text: str = "") -> str:
    stage = stage_of(state["affection"])
    scenario = detect_scenario(user_text) if user_text else "flirt" if stage_index(stage) >= 3 else "daily"
    is_group = state["chat_type"] == "group"

    parts = [CORE, LINE_STYLE]
    if is_group:
        parts.append(GROUP_STYLE)
    parts.append(f"# 目前好感度階段：{stage}\n{STAGE_TEXT.get(stage, '')}")
    parts.append(f"# 你自己的故事\n{BACKSTORY_RULES}\n\n{backstory_text(stage, is_group)}")
    if state["mode"] == "proactive":
        trigger = TRIGGER_TEXT.get(state.get("trigger") or "miss", state.get("trigger"))
        detail = state.get("trigger_detail") or ""
        parts.append(f"{PROACTIVE}\n本次觸發原因：{trigger}{('，' + detail) if detail else ''}")

    who = "這位發話者" if is_group else "他"
    parts.append(f"# 關於{who}\n{profile_text(state.get('profile') or {})}\n\n"
                 f"# 相關記憶\n{memories_text(state.get('memories') or [])}")
    if state.get("summary"):
        parts.append(f"# 先前對話摘要\n{state['summary']}")

    examples = "\n\n".join(f"對方：{e['user']}\n你：{e['reply']}" for e in pick_examples(scenario, stage))
    quotes = "\n".join(f"- {q}" for q in pick_quotes(scenario, stage))
    parts.append(f"# 範例（學語氣和格式，不要照抄）\n{examples}")
    parts.append(f"# 句庫（語感參考，可以化用，不要照抄）\n{quotes}")
    parts.append(f"# 現在時間\n{time_text(now)}")
    return "\n\n---\n\n".join(parts)
