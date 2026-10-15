# LINE 御姐聊天機器人：架構計畫

## Context
目前 [server.py](c:\Line聊天夥伴\server.py) 是最基本的版本：FastAPI webhook → 一次 OpenAI 呼叫（`gpt-4o-mini`，`instructions=character.md`）→ reply。每則訊息都是獨立處理，沒有上下文、沒有記憶，也不會主動傳訊。
目標是改寫成 LangGraph 架構，具備：
1. 參考最近的對話當上下文（短期記憶）
2. 記得使用者的個人資訊，例如喜歡的食物、生日（長期記憶）
3. 好感度機制，會依對話內容與互動行為升降，並影響語氣
4. 主動傳訊（例如「好想你」），頻率可以調整
5. 支援一對一和群組
6. 以 gentle-mature-skill 倉庫為主要人設基礎，改成帶點曖昧、寵溺的御姐

### 現有程式的問題（重構時一併修正）
- 用的是 `linebot.LineBotApi`（v2 SDK，已棄用），改成 `linebot.v3`
- `handle_message` 在 async endpoint 裡同步呼叫 LLM，會卡住 webhook，改成背景任務處理
- 沒有捕捉 `InvalidSignatureError`，缺少 `X-Line-Signature` 時會直接 500
- `replace("@AI", "")` 是舊邏輯的殘留，群組的 @ 偵測要改用 LINE 的 mention 物件

---

## 一、gentle-mature-skill 倉庫怎麼用

這個倉庫是 **Claude Code / Codex 的「Skill」**：agent 先讀 `SKILL.md`，需要時才去讀 `references/` 裡的檔案（漸進式載入）。它**不是可以直接貼上的 system prompt**，所以要拆開後重新組合：

| 倉庫檔案 | 在我們機器人裡的用法 |
|---|---|
| `SKILL.md` + `references/persona.md` | 改寫成主人設 `persona/core.md`：保留語氣、說話風格、稱呼、emoji 規則、「外柔內剛」；**移除**「不談戀愛／不承諾陪伴／避免依賴」；再併入你現有 `character.md` 的 Ara Ara 調侃風格 |
| `references/thinking_framework.md` | 放進 prompt 的「回應策略」段落：遇到情緒場景（低落／疲憊／迷茫／受傷）時走「接納→理解→陪伴→賦能」四步 |
| `references/examples.md` | 挑 2～3 組改寫成 few-shot 範例放進 prompt |
| `references/golden_quotes.json` | 每次回覆時依場景從句庫隨機抽 3 句，標註「語感參考，不要照抄」，讓語氣有變化又不會重複 |
| `metadata.json`、`agents/openai.yaml` | 用不到 |

處理方式：
- 原始檔用 `git clone` 放到 `persona/vendor/gentle-sister/`，保留出處（MIT 授權，需要標註來源）
- 倉庫是簡體中文，用 OpenCC（`s2twp`）轉成台灣繁體與台灣用語後再改寫
- **倉庫缺少、需要自己補的提示詞**：
  - `line_style.md`：LINE 聊天格式（短句、一次 1～3 個泡泡、用 `|||` 分隔再拆成多則訊息、不用 Markdown、不要條列）
  - `affection_levels.md`：各好感度階段的稱呼、親密程度、能不能說「想你」
  - 記憶使用規則：要像不經意想起來那樣提，不要一次把記得的東西全列出來
  - 主動訊息的指令（見第五節）
  - 時間感知（注入目前時間，深夜、早上、生日當天的語氣會不同）
  - 群組行為（語氣收斂一點、只回 @ 她的人）
  - 不要有 AI 腔（不說「身為 AI」、不寫客服式結尾）
- **安全規則（我的建議）**：你說危機處理可以拿掉，我照做，倉庫那一整段制式流程都會移除。不過我建議留**一句**角色內的規則：「若對方明確表達想傷害自己，先放下調情，認真關心並鼓勵他找身邊信任的人或撥 1925」。原因是好感度加上主動想念的設計，本身就會讓使用者產生情感依附，這一句成本很低，也不會破壞角色。如果不要，實作時刪掉就好。

---

## 二、整體架構

```
LINE ──webhook──> FastAPI /callback ──(背景任務)──> LangGraph(reply 模式) ──> reply_message
                     │
                     └─ lifespan 啟動 APScheduler ─每 15 分鐘─> proactive.tick()
                                                                 └─> LangGraph(proactive 模式) ──> push_message
儲存：SQLite（data/bot.sqlite）
  - LangGraph checkpointer：短期對話（thread）
  - LangGraph Store：長期記憶（個人資料 + 事件記憶，向量搜尋）
  - users / groups 資料表：好感度、最後活躍時間、主動訊息紀錄、推播計數
```

### 目錄結構
```
Line聊天夥伴/
├── server.py            # FastAPI、webhook、lifespan（啟動排程）
├── config.py            # 所有可調參數（模型、主動頻率、好感度規則、安靜時段、額度上限）
├── persona/
│   ├── vendor/gentle-sister/   # 倉庫原檔
│   ├── core.md                 # 主人設（取代 character.md）
│   ├── affection_levels.md
│   ├── line_style.md
│   └── quotes.json             # 轉繁中後的句庫
└── bot/
    ├── state.py         # ChatState
    ├── graph.py         # StateGraph 組裝與編譯
    ├── nodes.py         # load_context / respond / post_process / summarize
    ├── prompts.py       # 組裝 system prompt
    ├── memory.py        # Store 讀寫
    ├── affection.py     # 好感度計算與階段
    ├── proactive.py     # 主動訊息排程邏輯
    ├── line_io.py       # v3 SDK 包裝、mention 判斷、拆泡泡
    └── db.py            # users / groups 資料表
```

---

## 三、LangGraph 設計

### State
```python
class ChatState(MessagesState):
    mode: Literal["reply", "proactive"]
    chat_type: Literal["user", "group"]
    chat_id: str          # 一對一 = userId，群組 = groupId
    speaker_id: str       # 發話者的 userId（群組裡用來對應個人記憶）
    summary: str          # 較早對話的滾動摘要
    profile: dict         # 長期記憶：個人資料
    memories: list[str]   # 長期記憶：跟這輪相關的事件記憶
    affection: int
```

### 流程（reply 模式）
```
START → load_context → respond → END
                         └─(回覆送出後，在背景執行) post_process → summarize(條件式)
```
- **load_context**：從 Store 讀 `profile`，以這次訊息做語意搜尋，取 top-5 `memories`；從 DB 讀好感度
- **respond**：組好 prompt 後呼叫主模型。prompt = core 人設 + 好感度階段 + 記憶 + 摘要 + 抽樣句庫 + LINE 格式 + 目前時間，再加上 `trim_messages` 過的最近約 20 則訊息。這是使用者唯一要等的 LLM 呼叫
- **post_process**（背景執行，不影響回覆速度）：一次 structured output 呼叫，輸出：
  - `affection_delta`（−3～+3）與理由
  - `profile_updates`（姓名、暱稱、生日、喜好、討厭的東西……）
  - `new_memories`（值得記住的事件，例如「下週要面試」）
  然後寫回 Store 和 DB
- **summarize**：thread 超過 40 則時，把較舊的訊息併進 `summary`，再用 `RemoveMessage` 刪掉

### 記憶分層
| 層 | 存放位置 | key |
|---|---|---|
| 短期對話 | `SqliteSaver` checkpointer | `thread_id = "user:{userId}"` 或 `"group:{groupId}"` |
| 對話摘要 | 存在 state 的 `summary` | 同上 |
| 個人資料 | `SqliteStore`，namespace `("user", userId)`，key `profile` | 跟著人走，群組裡也是同一份 |
| 事件記憶 | `SqliteStore` + OpenAI embedding（`text-embedding-3-small`），namespace `("user", userId, "memories")` | 語意搜尋 |

群組：短期對話以群組為單位，個人記憶與好感度以**發話者**為單位。所以你在群組講過的事，私聊時她也記得。

---

## 四、好感度機制（`bot/affection.py` + `config.py`）
- 數值 0～100，新使用者從 30 開始
- 階段：陌生（0–20）→ 熟悉（21–45）→ 親近（46–70）→ 曖昧（71–89）→ 心動（90–100）。每個階段對應 `affection_levels.md` 的一段語氣說明，並決定一個主動訊息機率倍率
- 會**上升**的情況：關心她、分享生活、記得她說過的話、稱讚、持續聊天
- 會**下降**的情況：不禮貌、敷衍、冷落；主動訊息超過 N 小時沒回 −1；連續 3 天沒互動後每天 −1（下限是該階段的底，避免一下子掉太多）
- 防刷分：每則訊息最多 ±3，每天上升總量上限 +10
- 所有數字都放在 `config.py`，方便調整

---

## 五、主動傳訊（`bot/proactive.py`）
- 在 FastAPI `lifespan` 啟動 `AsyncIOScheduler`，每 15 分鐘執行一次 `tick()`
- 對每個「已加好友、一對一」的使用者檢查以下條件，全部通過才擲骰：
  1. 不在安靜時段（預設 23:30～08:30）
  2. 今天的主動訊息還沒達到上限（`PROACTIVE_MAX_PER_DAY`）
  3. 距離使用者上一則訊息 ≥ `MIN_IDLE_HOURS`（剛聊過就不發）
  4. 距離上一則主動訊息 ≥ `MIN_GAP_HOURS`，而且上一則主動訊息使用者已經回過（避免連發）
  5. 本月推播總數還沒到 `MONTHLY_PUSH_BUDGET`（另外會用 LINE 的 quota API 核對）
- 機率 = `BASE_PROB × 好感度倍率`：好感度越高，越常主動找你；陌生階段幾乎不會
- **事件觸發**（不擲骰，直接發）：記憶裡的生日當天早上、使用者說過的重要事件當天（例如「面試加油」）
- 產生內容：用 `mode="proactive"` 呼叫同一張 graph，respond 節點換成主動指令：「根據記憶、時間、上次聊到的話題，自然地開啟話題或表達想念；依好感度階段決定直白程度」。產生的訊息寫進同一個 thread，所以使用者回覆時她知道自己剛剛說了什麼
- 群組預設不主動發訊（`PROACTIVE_IN_GROUPS = False`）
- `config.py` 提供一組預設值，你之後可以自己調

---

## 六、LINE 對接（`server.py` + `bot/line_io.py`）
- 改用 `linebot.v3`（`AsyncMessagingApi`、`WebhookParser`）
- webhook 驗證簽章後立刻回 200，用 `BackgroundTasks` 處理訊息
- 一對一：處理前先呼叫 loading animation API，畫面會顯示「輸入中」
- 群組：只在 mention 到 bot（用 `mention.mentionees` 比對 bot 的 userId）或訊息含暱稱時才回應
- 回覆時用 `|||` 把模型輸出拆成最多 5 則 TextMessage，看起來像真人連續傳訊
- reply token 過期時（處理太久）改用 push 補送
- 處理 `FollowEvent` / `UnfollowEvent` / `JoinEvent`，更新 users / groups 資料表（主動傳訊只發給還是好友的人）

---

## 七、套件
`langgraph`、`langchain-openai`、`langgraph-checkpoint-sqlite`（內含 SqliteSaver / SqliteStore）、`line-bot-sdk>=3`、`apscheduler`、`opencc-python-reimplemented`、`fastapi`、`uvicorn`、`python-dotenv`
（裝在你現有的 conda 環境；模型沿用 `gpt-4o-mini`，在 `config.py` 可以換）

## 八、實作順序
1. 人設：clone 倉庫 → 轉繁中 → 寫 `core.md`、`line_style.md`、`affection_levels.md`、`quotes.json`
2. 改用 v3 SDK + 背景任務，並建立 graph（只有 load_context→respond），加上 SqliteSaver → 達成「有上下文的對話」
3. 長期記憶：Store + post_process 抽取 + summarize
4. 好感度
5. 群組支援
6. 主動傳訊排程

## 九、驗證方式
- **離線測試**：寫 `scripts/chat_cli.py`，在終端機直接呼叫 graph 模擬對話（不需要 LINE），確認：
  - 先說「我最喜歡吃拉麵，生日是 3/14」，開新的一輪再問「你記得我喜歡吃什麼嗎」→ 要答得出來
  - 連續稱讚、連續敷衍，觀察好感度和語氣的變化
  - 對話超過 40 則後摘要是否產生、舊訊息是否被刪除
- **主動訊息**：`scripts/proactive_dryrun.py`，把時間、條件、機率設成必中，只印出產生的訊息、不真的 push；確認安靜時段和每日上限有生效
- **端對端**：`python server.py` + `ngrok http 8002`，在 LINE Developers 設定 webhook URL，用手機測一對一、群組 @、加好友／封鎖，最後把主動頻率調高，實測一次 push
