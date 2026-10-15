# LINE 御姐聊天機器人：架構計畫（v2）

## Context
目前 [server.py](c:\Line聊天夥伴\server.py) 是最基本的版本：FastAPI webhook → 一次 OpenAI 呼叫（`gpt-4o-mini`，`instructions=character.md`）→ reply。每則訊息都是獨立處理，沒有上下文、沒有記憶，也不會主動傳訊。
目標是改寫成 LangGraph 架構，具備：
1. 參考最近的對話（短期記憶）
2. 記得使用者的個人資訊（長期記憶）
3. 好感度，會升降並影響語氣，要到心動階段不容易
4. 主動傳訊，頻率可以調
5. 一對一 + 群組（群組只在明確 @ 她時回應）
6. 以 gentle-mature-skill 倉庫為人設基礎，全部轉成繁體中文，改成曖昧、寵溺的御姐

### 現有程式的問題（重構時一併修正）
- v2 SDK（`LineBotApi`）已棄用 → 改用 `linebot.v3`
- async endpoint 裡同步呼叫 LLM → 改成背景任務
- 沒有處理 `InvalidSignatureError` 和缺少簽章 header 的情況
- `replace("@AI", "")` 是殘留邏輯，群組改用 mention 物件判斷

---

## 一、人設來源（gentle-mature-skill）
這個倉庫是 Claude Code / Codex 的 Skill（漸進式載入），不能直接當 system prompt 用，要拆開後重新組合。

**步驟**
1. `git clone` 到 `persona/vendor/gentle-sister/`，原檔不動，保留 MIT 出處
2. **全部轉成繁體中文**：先用 OpenCC `s2twp` 轉字和詞彙，再人工潤成台灣口語，放到 `persona/source_zh_tw/`。要轉的檔案：`SKILL.md`、`references/persona.md`、`references/thinking_framework.md`、`references/examples.md`、`references/golden_quotes.json`（`metadata.json`、`agents/openai.yaml` 用不到，不轉）
3. 根據繁中版產生實際執行時用的檔案：

| 來源 | 執行時檔案 | 用法 |
|---|---|---|
| SKILL.md + persona.md + 你的 character.md | `persona/core.md` | 主人設。保留語氣、稱呼、emoji 規則、外柔內剛；移除「不戀愛／不承諾陪伴／避免依賴」；加入 Ara Ara 調侃 |
| thinking_framework.md | 併入 `core.md` 的「回應策略」 | 情緒場景走「接納→理解→陪伴→賦能」 |
| examples.md | `persona/examples.json`（**全部轉換，依場景標記**） | 每輪依場景**動態挑 2～3 組**當 few-shot |
| golden_quotes.json | `persona/quotes.json` | 每輪依場景隨機抽 3 句當語感參考 |

**自己補的提示詞**：`line_style.md`（短句、`|||` 分泡泡、不用 Markdown）、`affection_levels.md`（各階段語氣）、記憶使用規則、主動訊息指令、時間感知、群組行為、去 AI 腔
**安全**：倉庫的危機處理流程整段移除，只保留一句角色內規則（對方明確表達想傷害自己時，先認真關心並提 1925）。不要的話可以刪掉

---

## 二、整體架構
```
LINE ─webhook─> /callback ─驗簽、立即回 200─> 背景任務
                                               ├ markAsRead（可設定是否、延遲）
                                               ├ loading animation（僅一對一）
                                               ├ reply graph ─> reply_message（1 次送出最多 5 泡泡）
                                               └ post_process（好感度、記憶抽取、摘要）
lifespan ─> APScheduler
             ├ 每天 00:05 plan_day()：替每位使用者排好今天要主動發的時間點
             └ 每 5 分鐘 tick()：時間到 + 條件檢查通過 ─> proactive graph ─> push_message
SQLite（data/bot.sqlite）：checkpointer（thread）、Store（長期記憶）、users/groups/proactive_schedule 資料表
```

### 目錄結構
```
Line聊天夥伴/
├── server.py  config.py
├── persona/  vendor/  source_zh_tw/  core.md  affection_levels.md  line_style.md  examples.json  quotes.json
├── bot/  state.py  graph.py  nodes.py  prompts.py  memory.py  affection.py  proactive.py  line_io.py  db.py
└── scripts/  chat_cli.py  proactive_dryrun.py
```

---

## 三、LangGraph 流程
```
                  ┌── mode == "reply" ─────> respond_reply ─────┐
START → load_context                                            ├─> END
                  └── mode == "proactive" ─> respond_proactive ─┘

（graph 外，reply 回完之後在背景執行）post_process → 視情況 summarize
```
- **load_context**：讀 profile（整份）、用語意搜尋從記憶庫取最相關的 `MEMORY_TOP_K` 筆、好感度、摘要
- **respond_reply**：輸入是使用者這次的訊息 + 最近 `SHORT_TERM_WINDOW` 則對話
- **respond_proactive**：沒有使用者輸入，改用主動指令（觸發原因：想念／生日／事件追蹤、現在時間、上次聊到什麼、好感度階段），由她自己開話題
- 兩個 respond 節點產生的 AI 訊息都會存進同一個 **thread**
- **thread**：LangGraph checkpointer 裡一段對話的完整紀錄，用 `thread_id` 區分。一對一是 `user:{userId}`，群組是 `group:{groupId}`

### State
`messages`、`mode`、`chat_type`、`chat_id`、`speaker_id`、`summary`、`profile`、`memories`、`affection`、`trigger`（主動訊息的觸發原因）

### 記憶
| 層 | 存放 | key | 可調參數 |
|---|---|---|---|
| 短期 | SqliteSaver | thread_id | `SHORT_TERM_WINDOW=30`，超過 `SUMMARIZE_AT=60` 則時，把較舊的訊息併進摘要 |
| 個人資料 | SqliteStore `("user", userId)` | 跟著 userId（一對一和所有群組共用） | — |
| 事件記憶 | SqliteStore + embedding `("user", userId, "memories")` | 同上，每筆標記 `source: private / group:{id}` | `MEMORY_TOP_K=5` |

**隱私規則**：在群組回覆時，不會提到私聊時得知的記憶（只用 profile 的基本資訊，加上同一個群組來源的記憶）

---

## 四、好感度（設計成慢熱）
- 0～100，新使用者 **30**（落在「熟悉」區間的低端）：對方是主動加好友，所以跳過陌生期；但又離「陌生」很近，態度不好會掉下去
- 階段：陌生 0–20｜熟悉 21–45｜親近 46–70｜曖昧 71–89｜心動 90–100
- 每則訊息由 post_process 評分 −3～+3；一般閒聊通常是 0 或 +1，只有特別貼心的互動才會 +2/+3
- **每日上升上限，隨階段遞減**：熟悉 +8、親近 +5、曖昧 +3、心動 +2
- **門檻條件**：進入曖昧需要認識 ≥ 14 天且互動天數 ≥ 7；進入心動需要認識 ≥ 30 天且互動天數 ≥ 20。沒達到門檻時，數值卡在該階段上限
- 估算：30→90 就算每天都拿滿上限也要約 14 天以上，加上門檻至少 30 天；一般使用者大概 1～2 個月
- 下降：不禮貌或敷衍（評分扣分）、主動訊息超過 `IGNORE_HOURS` 沒回 −1、連續 3 天沒互動後每天 −1（不會低於該階段的下限）
- 所有數字都放在 `config.py`

---

## 五、主動傳訊
**改用「每日排程」**（取代原本每 15 分鐘擲骰的 `BASE_PROB`，比較好理解也好控制）：
- `plan_day()`：每天 00:05 依好感度階段的「每日期望則數」`PROACTIVE_DAILY_EXPECTED`（陌生 0、熟悉 0.3、親近 0.6、曖昧 1.0、心動 1.5）決定今天發幾則（上限 `PROACTIVE_MAX_PER_DAY`），在非安靜時段隨機挑時間點，寫進 `proactive_schedule`
- `tick()`：每 5 分鐘檢查已經到點的排程。發送前用純 Python 再檢查一次（不花 LLM 費用）：
  - 現在不是安靜時段
  - 距離使用者上一則訊息 ≥ `MIN_IDLE_HOURS`
  - 上一則主動訊息使用者已經回過
  - 本月推播還沒到 `MONTHLY_PUSH_BUDGET`
  全部通過才呼叫 `mode="proactive"` 的 graph 並 push；沒通過就跳過這個時間點
- 例子：00:05 排出「你｜14:23｜等待中」→ 14:20 的 tick 還沒到點，不做事 → 14:25 的 tick 發現已經到點，檢查條件，通過就發送並標成「已發送」，不通過就跳過。最多晚 `TICK_MINUTES`（預設 5）分鐘發出；用固定間隔檢查，而不是替每個人設精準鬧鐘，是因為伺服器重開後不用重建排程，比較穩定
- **事件觸發**：生日當天早上、記憶中重要事件當天，排程時一定會排進去
- **新好友**：加好友時用 reply 傳歡迎訊息（免費）。主動訊息要等「累計 ≥ `PROACTIVE_MIN_USER_MSGS`（預設 20）則訊息」而且階段 ≥ 熟悉才會開始
- 群組不主動發訊

---

## 六、LINE 對接
- 改用 `linebot.v3`；驗簽後立即回 200，其餘交給背景任務
- **已讀**：你目前「不會自動已讀」，是因為 OA Manager 開著「聊天」功能。loading animation **不會**讓訊息變已讀。改用官方的 `POST /v2/bot/chat/markAsRead`（webhook 訊息事件裡附有 `markAsReadToken`，不用另外申請）由程式控制已讀
  - `MARK_AS_READ=True`、`READ_DELAY_SEC=(1,4)`：隨機延遲後才已讀，接著顯示 loading，再回覆
- **loading animation**：只能在一對一使用，5～60 秒，而且只有使用者正在看聊天畫面時才會顯示
- **群組**：只有 `mention.mentionees` 裡有 bot 的 userId（或 `isSelf`）時才回應，不比對暱稱
- **reply token**：只能用一次，必須在收到 webhook 後 1 分鐘內使用。所以所有泡泡都要在一次 reply 裡送出（最多 5 則）。超過約 50 秒還沒處理完，就改用 push 補送（會消耗額度）
- Follow / Unfollow / Join / Leave 事件會更新資料表
- 提醒：要關掉 OA Manager 的「加入好友的歡迎訊息」和「自動回應訊息」，避免跟 bot 重複回覆

---

## 七、套件
`langgraph`、`langchain-openai`、`langgraph-checkpoint-sqlite`、`line-bot-sdk>=3`、`apscheduler`、`opencc-python-reimplemented`、`fastapi`、`uvicorn`、`python-dotenv`（裝在現有的 conda 環境，模型在 `config.py` 設定，預設 `gpt-4o-mini`）

## 八、實作順序
1. 人設：clone → 繁中化 → `core.md` / `examples.json` / `quotes.json` / `line_style.md` / `affection_levels.md`
2. v3 SDK + 背景任務 + 已讀 / loading + graph（load_context → respond_reply）+ SqliteSaver
3. 長期記憶 + post_process + summarize
4. 好感度
5. 群組（只回應 mention）
6. 主動傳訊（plan_day / tick）+ 歡迎訊息

## 九、驗證
- `scripts/chat_cli.py`：終端機模擬對話。確認記得「拉麵、3/14 生日」；好感度升降、每日上限、門檻有效；超過 60 則後有產生摘要
- `scripts/proactive_dryrun.py`：模擬時間與好感度，只印出排程和產生的訊息，不真的 push
- 端對端：`python server.py` + `ngrok http 8002`，在手機上測：一對一（已讀延遲、loading、多泡泡）、群組只回應 @、加好友歡迎訊息、主動 push 一次
