"""graph 節點，以及回覆送出後在背景執行的 post_process / summarize。"""
import re
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

import config
from bot import clock, memory
from bot.prompts import build_system_prompt, time_text
from bot.state import ChatState


def _text(msg: BaseMessage) -> str:
    """取出訊息的文字；含圖片的訊息（list content）只取文字部分。"""
    if isinstance(msg.content, str):
        return msg.content
    return " ".join(p.get("text", "") for p in msg.content if isinstance(p, dict) and p.get("type") == "text")


def has_image(msg: BaseMessage) -> bool:
    return isinstance(msg.content, list) and any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in msg.content)


def window_size(chat_type: str) -> int:
    return config.GROUP_SHORT_TERM_WINDOW if chat_type == "group" else config.SHORT_TERM_WINDOW


ALLOWED_EMOJI = {"🤗", "💕", "🌸", "☀", "🍀", "🌙"}
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]️?")


def _clean_reply(text: str) -> str:
    text = text.strip()
    for prefix in ("你：", "姐姐："):
        if text.startswith(prefix):
            text = text[len(prefix):]
    # 只保留白名單 emoji，且最多 2 個
    kept = 0

    def keep(m):
        nonlocal kept
        if m.group(0).rstrip("️") in ALLOWED_EMOJI and kept < 2:
            kept += 1
            return m.group(0)
        return ""

    text = EMOJI_RE.sub(keep, text).strip()
    return _casual_punctuation(text) if config.CASUAL_PUNCTUATION else text


def _casual_punctuation(text: str) -> str:
    """逗號改空格、拿掉句號（句中的句號也改成空格）。逐個泡泡、逐行處理。"""
    def fix(line: str) -> str:
        line = line.replace("，", " ").replace("。", " ")
        return re.sub(r"[ 　]{2,}", " ", line).strip()

    # 泡泡內的換行也改成空格，避免 1 個泡泡看起來像兩段
    bubbles = [" ".join(fix(line) for line in b.split("\n") if line.strip()) for b in text.split("|||")]
    return "|||".join(b for b in bubbles if b)


class Nodes:
    def __init__(self, chat_llm, store, db):
        self.chat_llm = chat_llm
        self.store = store
        self.db = db

    def load_context(self, state: ChatState) -> dict:
        speaker = state.get("speaker_id")
        if not speaker:
            return {"profile": {}, "memories": [], "affection": config.AFFECTION_INIT}
        last = state["messages"][-1] if state["messages"] else None
        query = _text(last) if isinstance(last, HumanMessage) else "最近的生活與心情"
        group_id = state["chat_id"] if state["chat_type"] == "group" else None
        return {
            "profile": memory.get_profile(self.store, speaker),
            "memories": memory.search_memories(self.store, speaker, query, config.MEMORY_TOP_K, group_id),
            "affection": self.db.get_user(speaker)["affection"],
        }

    def route(self, state: ChatState) -> str:
        return "respond_proactive" if state["mode"] == "proactive" else "respond_reply"

    def respond_reply(self, state: ChatState) -> dict:
        user_text = _text(state["messages"][-1])
        system = build_system_prompt(state, clock.now(), user_text)
        window = state["messages"][-window_size(state["chat_type"]):]
        result = self.chat_llm.invoke([SystemMessage(system), *window])
        return {"messages": [AIMessage(_clean_reply(_text(result)))]}

    def respond_proactive(self, state: ChatState) -> dict:
        system = build_system_prompt(state, clock.now())
        window = state["messages"][-config.SHORT_TERM_WINDOW:]
        # 他還沒回的、你之前主動傳的訊息：明確列出來，避免模型照抄自己上一則
        unanswered = []
        for m in reversed(window):
            if isinstance(m, HumanMessage):
                break
            unanswered.insert(0, _text(m).replace("|||", " "))
        note = ""
        if unanswered:
            listed = "\n".join(f"- {t}" for t in unanswered)
            note = f"\n他還沒回覆你之前傳的這些訊息：\n{listed}\n這次換個話題或角度，不要重複上面的內容與句子。"
        cue = SystemMessage(f"（現在是 {time_text(clock.now())}，內容要符合這個時間。"
                            f"請你主動傳訊息給他，只輸出訊息內容。）{note}")
        result = self.chat_llm.invoke([SystemMessage(system), *window, cue])
        return {"messages": [AIMessage(_clean_reply(_text(result)))]}


# ---------------- 背景：好感度評分 + 記憶抽取 ----------------

class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(None, description="他的名字，沒提到就留空")
    nickname: Optional[str] = Field(None, description="他希望被怎麼稱呼")
    birthday: Optional[str] = Field(None, description="生日，格式 MM-DD")
    likes: list[str] = Field(default_factory=list, description="新提到喜歡的東西")
    dislikes: list[str] = Field(default_factory=list, description="新提到不喜歡的東西")
    facts: list[str] = Field(default_factory=list, description="長期不變的個人資訊，例如職業、住哪、養的寵物")


class MemoryItem(BaseModel):
    text: str = Field(description="值得記住的事件，用第三人稱寫，例如「他下週三要面試」")
    date: Optional[str] = Field(None, description="若是未來某天會發生的事，填該日期 YYYY-MM-DD，否則留空")


class PostResult(BaseModel):
    affection_delta: int = Field(description="這則訊息讓好感度變化 -3~+3；一般閒聊 0 或 +1")
    reason: str = Field(description="評分理由，一句話")
    profile_updates: ProfileUpdate
    new_memories: list[MemoryItem] = Field(default_factory=list)


POST_PROMPT = """你是聊天機器人「溫柔御姐」的後台分析員。根據對話，輸出：
1. affection_delta：他最新這則訊息讓御姐的好感度變化多少（-3~+3）。
   +1：正常友善的聊天、分享生活。+2：主動關心她、記得她說過的話、真誠讚美。+3：非常貼心、令人心動的互動（很少見）。
   0：普通、簡短的回應。-1：敷衍、冷淡。-2：不禮貌。-3：辱罵、惡意。
   預設保守評分，大多數訊息應該是 0 或 +1。
2. profile_updates：只填這次「新提到」的個人資料；已經在現有資料裡的不要重複。
3. new_memories：值得之後提起的事件或心情（例如最近在忙的事、即將發生的事）。瑣碎寒暄不用記。沒有就給空陣列。

今天是 {today}（用來把「下週三」這類說法換成日期）。

現有個人資料：
{profile}

最近對話：
{dialog}

他最新的訊息：{user_text}
御姐的回覆：{ai_text}"""


def run_post_process(analyze_llm, store, db, affection_mod, *, user_id: str, source: str,
                     user_text: str, ai_text: str, recent: list[BaseMessage]) -> dict:
    now = clock.now()
    dialog = "\n".join(f"{'他' if isinstance(m, HumanMessage) else '御姐'}：{_text(m)}" for m in recent[-8:])
    prompt = POST_PROMPT.format(today=now.date().isoformat(), profile=memory.get_profile(store, user_id) or "（無）",
                                dialog=dialog, user_text=user_text, ai_text=ai_text)
    result: PostResult = analyze_llm.with_structured_output(PostResult).invoke(prompt)

    new_score, change = affection_mod.apply_delta(db, user_id, result.affection_delta, now)
    memory.update_profile(store, user_id, result.profile_updates.model_dump())
    added = memory.add_memories(store, user_id, [m.model_dump() for m in result.new_memories], source, now)
    return {"delta": result.affection_delta, "applied": change, "affection": new_score,
            "reason": result.reason, "profile": result.profile_updates.model_dump(exclude_defaults=True),
            "memories_added": added}


SUMMARY_PROMPT = """把以下較早的聊天紀錄整理成精簡的摘要（繁體中文，200 字以內），保留：聊過的重要話題、他的心情變化、你們之間的互動氛圍、還沒聊完的事。
{previous}
要併入的對話：
{dialog}"""

GROUP_SUMMARY_PROMPT = """把以下較早的 LINE 群組聊天紀錄整理成精簡的摘要（繁體中文，250 字以內）。每則訊息前的 [名字] 是發話者，「御姐」是你自己。
保留：群組在聊的主要話題、誰說了什麼重要的事（寫出名字）、大家和御姐的互動、還沒聊完的事。瑣碎的寒暄、貼圖可以省略。
{previous}
要併入的對話：
{dialog}"""


def summarize_messages(analyze_llm, previous: str, messages: list[BaseMessage], chat_type: str = "user") -> str:
    is_group = chat_type == "group"
    speaker = "" if is_group else "對方："  # 群組訊息本身已帶 [名字]
    dialog = "\n".join(f"{speaker}{_text(m)}" if isinstance(m, HumanMessage) else f"御姐：{_text(m)}"
                       for m in messages)
    prev = f"既有摘要：\n{previous}\n" if previous else ""
    prompt = (GROUP_SUMMARY_PROMPT if is_group else SUMMARY_PROMPT).format(previous=prev, dialog=dialog)
    return _text(analyze_llm.invoke(prompt)).strip()


DESCRIBE_IMAGE_PROMPT = ("用一句繁體中文客觀描述這張照片的內容（地點、人事物、在做什麼、氛圍），"
                         "40 字以內，只輸出描述。")


def describe_image(analyze_llm, image_msg: BaseMessage) -> str:
    """把照片描述成一句話，用來取代 thread 裡的原圖（避免之後每次回覆都重送圖片）。"""
    image_parts = [p for p in image_msg.content if isinstance(p, dict) and p.get("type") == "image_url"]
    result = analyze_llm.invoke([HumanMessage(content=[{"type": "text", "text": DESCRIBE_IMAGE_PROMPT}, *image_parts])])
    return _text(result).strip()
