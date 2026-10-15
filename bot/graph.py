"""組裝 LangGraph，並包成 Companion，供 server / 測試腳本 / 主動排程共用。

                  ┌── reply ─────> respond_reply ─────┐
START → load_context                                    ├─> END
                  └── proactive ─> respond_proactive ─┘
（graph 外，回覆送出後）after_reply：post_process（好感度、記憶）→ 視情況 summarize
"""
import logging
import sqlite3

from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.sqlite import SqliteStore

import config
from bot import affection, nodes
from bot.db import DB
from bot.state import ChatState

log = logging.getLogger(__name__)


def thread_id(chat_type: str, chat_id: str) -> str:
    return f"{chat_type}:{chat_id}"


class Companion:
    def __init__(self, db: DB | None = None, graph_db_path=config.GRAPH_DB_PATH):
        graph_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = db or DB()
        self.chat_llm = ChatOpenAI(model=config.CHAT_MODEL, temperature=config.CHAT_TEMPERATURE)
        self.analyze_llm = ChatOpenAI(model=config.ANALYZE_MODEL, temperature=0)

        store_conn = sqlite3.connect(graph_db_path, check_same_thread=False, isolation_level=None)
        self.store = SqliteStore(store_conn, index={
            "dims": config.EMBED_DIMS,
            "embed": OpenAIEmbeddings(model=config.EMBED_MODEL),
            "fields": ["text"],
        })
        self.store.setup()
        saver_conn = sqlite3.connect(graph_db_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(saver_conn)
        self.checkpointer.setup()

        n = nodes.Nodes(self.chat_llm, self.store, self.db)
        builder = StateGraph(ChatState)
        builder.add_node("load_context", n.load_context)
        builder.add_node("respond_reply", n.respond_reply)
        builder.add_node("respond_proactive", n.respond_proactive)
        builder.add_edge(START, "load_context")
        builder.add_conditional_edges("load_context", n.route, ["respond_reply", "respond_proactive"])
        builder.add_edge("respond_reply", END)
        builder.add_edge("respond_proactive", END)
        self.graph = builder.compile(checkpointer=self.checkpointer, store=self.store)

    @staticmethod
    def _config(chat_type: str, chat_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id(chat_type, chat_id)}}

    def reply(self, *, chat_type: str, chat_id: str, speaker_id: str | None, text: str) -> str:
        out = self.graph.invoke(
            {"messages": [HumanMessage(text)], "mode": "reply", "chat_type": chat_type,
             "chat_id": chat_id, "speaker_id": speaker_id, "trigger": None, "trigger_detail": None},
            self._config(chat_type, chat_id),
        )
        return out["messages"][-1].content

    def proactive(self, *, user_id: str, trigger: str, detail: str | None = None) -> str:
        out = self.graph.invoke(
            {"messages": [], "mode": "proactive", "chat_type": "user", "chat_id": user_id,
             "speaker_id": user_id, "trigger": trigger, "trigger_detail": detail},
            self._config("user", user_id),
        )
        return out["messages"][-1].content

    def post_process(self, *, chat_type: str, chat_id: str, speaker_id: str | None,
                     user_text: str, ai_text: str) -> dict | None:
        """好感度評分 + 記憶抽取。"""
        if not speaker_id:
            return None
        messages = self.graph.get_state(self._config(chat_type, chat_id)).values.get("messages", [])
        source = "private" if chat_type == "user" else f"group:{chat_id}"
        result = nodes.run_post_process(
            self.analyze_llm, self.store, self.db, affection,
            user_id=speaker_id, source=source, user_text=user_text, ai_text=ai_text, recent=messages,
        )
        log.info("post_process %s: %s", speaker_id, result)
        return result

    def after_reply(self, **kwargs) -> dict | None:
        """回覆送出後執行：post_process，以及必要時壓縮摘要。"""
        result = self.post_process(**kwargs)
        self.summarize_if_needed(kwargs["chat_type"], kwargs["chat_id"])
        return result

    def summarize_if_needed(self, chat_type: str, chat_id: str) -> bool:
        cfg = self._config(chat_type, chat_id)
        values = self.graph.get_state(cfg).values
        messages = values.get("messages", [])
        if len(messages) <= config.SUMMARIZE_AT:
            return False
        older = messages[:-config.SHORT_TERM_WINDOW]
        summary = nodes.summarize_messages(self.analyze_llm, values.get("summary", ""), older)
        self.graph.update_state(
            cfg, {"summary": summary, "messages": [RemoveMessage(id=m.id) for m in older]},
            as_node="respond_reply",
        )
        log.info("summarized %s: %d messages", thread_id(chat_type, chat_id), len(older))
        return True

    def history(self, chat_type: str, chat_id: str):
        return self.graph.get_state(self._config(chat_type, chat_id)).values
