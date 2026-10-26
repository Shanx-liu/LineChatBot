"""所有可調參數集中在這裡。改完重啟 server 即生效。"""
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).parent
PERSONA_DIR = BASE_DIR / "persona"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bot.sqlite"          # 使用者資料表（好感度、排程…）
GRAPH_DB_PATH = DATA_DIR / "graph.sqlite"  # LangGraph checkpointer + 長期記憶 Store

TZ = ZoneInfo("Asia/Taipei")
PORT = 8002

# ---------- 模型 ----------
CHAT_MODEL = "gpt-5.4-mini"          # 回覆用
CHAT_TEMPERATURE = 0.9
ANALYZE_MODEL = "gpt-4o-mini"       # 好感度評分、記憶抽取、摘要用
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536

# ---------- 短期記憶 ----------
SHORT_TERM_WINDOW = 30   # 每次回覆帶入最近幾則訊息
SUMMARIZE_AT = 60        # thread 超過幾則時，把較舊的訊息壓縮成摘要

# ---------- 群組上下文 ----------
GROUP_CONTEXT_ENABLED = True    # 群組裡沒 @ 她的訊息也記進群組對話紀錄（不進個人記憶、不影響好感度）
GROUP_SHORT_TERM_WINDOW = 40    # 群組每次回覆帶入最近幾則訊息
GROUP_SUMMARIZE_AT = 100        # 群組 thread 超過幾則時壓縮摘要

# ---------- 圖片 ----------
IMAGE_ENABLED = True            # 一對一時看得懂使用者傳的照片
IMAGE_DETAIL = "low"            # "low" 便宜、"high" 看得清楚細節（費用較高）

# ---------- 長期記憶 ----------
MEMORY_TOP_K = 5         # 每次回覆從記憶庫取最相關的幾筆

# ---------- 人設取樣 ----------
FEW_SHOT_COUNT = 3       # 每次挑幾組範例
QUOTE_COUNT = 3          # 每次抽幾句句庫

# ---------- 好感度 ----------
AFFECTION_INIT = 30
# (名稱, 下限, 上限)；名稱要和 persona/affection_levels.md 的標題一致
AFFECTION_STAGES = [
    ("陌生", 0, 20),
    ("熟悉", 21, 45),
    ("親近", 46, 70),
    ("曖昧", 71, 89),
    ("心動", 90, 100),
]
DELTA_PER_MSG = 3                       # 每則訊息評分上下限 ±3
DAILY_GAIN_CAP = {"陌生": 8, "熟悉": 8, "親近": 5, "曖昧": 3, "心動": 2}
STAGE_GATES = {                         # 進入該階段的門檻
    "曖昧": {"days_known": 14, "active_days": 7},
    "心動": {"days_known": 30, "active_days": 20},
}
INACTIVE_DECAY_AFTER_DAYS = 3           # 連續幾天沒互動後開始每天 -1（不低於該階段下限）
IGNORE_HOURS = 12                       # 主動訊息超過幾小時沒回 -1

# ---------- 主動傳訊 ----------
PROACTIVE_ENABLED = True
PROACTIVE_DAILY_EXPECTED = {"陌生": 0, "熟悉": 0.3, "親近": 0.6, "曖昧": 1.0, "心動": 1.5}
PROACTIVE_MAX_PER_DAY = 2
PROACTIVE_MIN_USER_MSGS = 20            # 累計幾則訊息後才開始主動傳訊
MIN_IDLE_HOURS = 3                      # 使用者上一則訊息後至少隔幾小時才主動
MIN_GAP_HOURS = 5                       # 「想念」訊息距離上一則主動訊息至少幾小時
QUIET_START = "23:30"                   # 安靜時段開始
QUIET_END = "08:30"                     # 安靜時段結束
MONTHLY_PUSH_BUDGET = 150               # 每月主動推播上限（LINE 免費方案 200 則/月，留點餘裕）
PLAN_DAY_AT = "00:05"                   # 每天幾點排今天的主動訊息
TICK_MINUTES = 5                        # 每幾分鐘檢查一次到點的排程
SCHEDULE_EXPIRE_MINUTES = 60            # 排程過期多久就放棄（例如伺服器關機錯過）
BIRTHDAY_SEND_AT = "09:00"
EVENT_SEND_WINDOW = ("08:45", "11:00")  # 事件追蹤訊息隨機落在這個區間

# ---------- LINE ----------
MARK_AS_READ = True
READ_DELAY_SEC = (1, 4)                 # 收到訊息後隨機延遲幾秒才已讀
LOADING_SECONDS = 20                    # 「輸入中」動畫秒數（5~60，5 的倍數）
REPLY_TOKEN_SAFE_SEC = 50               # 超過這個秒數就改用 push（reply token 1 分鐘失效）
MAX_BUBBLES = 2                         # 一次最多幾個泡泡，多出來的併進最後一個（LINE 上限 5）
TWO_BUBBLE_RATE = 0.3                   # 模型回 2 個以上泡泡時，保留的機率；其餘合併成 1 個（0 = 永遠只回 1 個）
CASUAL_PUNCTUATION = True               # 年輕人打字習慣：逗號改空格、句尾不加句號（程式強制轉換）
