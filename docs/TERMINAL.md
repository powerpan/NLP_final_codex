# 互動終端

我們保留原來的 SQLite、Runtime、工具授權和 Docker 沙箱，在其上增加持續對話介面。左側顯示對話及串流草稿，右側顯示當輪父子任務樹，人工問題和操作審批出現在輸入框上方。視窗較窄時，任務樹可透過 `/tree` 查看。

## 啟動

完整介面需要可選套件 `prompt_toolkit`。在專案目錄安裝一次：

```zsh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[terminal]'
```

先用獨立資料庫體驗離線流程：

```zsh
zsh scripts/terminal.zsh --demo --db data/terminal-demo.sqlite
```

輸入「協作」。示範會讓主 Agent 委派 researcher 和 coder；researcher 再委派 reviewer，主 Agent 同時取得一次唯讀諮詢。coder 會提出問題，再顯示一個 Git 推送審批。直接回答問題，然後按畫面中的 ID 輸入 `/approve ID` 或 `/deny ID`，即可看見子任務交回結果。這是固定腳本，批准也不會真的推送；全程不呼叫模型、網頁服務或執行生成程式。

真實使用時換一個資料庫並移除 `--demo`：

```zsh
zsh scripts/terminal.zsh --db data/terminal-live.sqlite
```

沒有 `DEEPSEEK_API_KEY` 環境變數時，終端會隱藏輸入 key。手動輸入的 key 只保留在本程序記憶體，重啟需要重新輸入；不要把 key 貼進對話輸入框。互動模式預設模型是 `deepseek-flash`，可先設定 `DEEPSEEK_MODEL` 換成帳戶可用的模型 ID。開啟畫面本身不發送模型請求，送出任務或恢復未完成工作後才會執行。

安裝後也可直接執行 `.venv/bin/python -m maplatform`；不帶參數會進入互動模式，預設資料庫為 `data/platform.sqlite`。指定資料庫時用 `.venv/bin/python -m maplatform shell --db 路徑`。`shell --plain` 提供行式介面，不依賴 `prompt_toolkit`；輸入或輸出不是 TTY 時也使用行式介面。

涉及檔案或程式執行仍需啟動 Docker，並事先安裝 `python:3.12-slim`。純文字對話可直接開始。沙箱不可用時會回報錯誤，不會改到宿主執行程式。Git 配置和產物匯出沿用 [操作文件](OPERATIONS.md)。

## 在同一個畫面工作

| 操作 | 用途 |
|---|---|
| Enter／Alt+Enter | 送出／換行 |
| Tab | 補全指令 |
| F2 | 輪流查看各任務詳情及主對話 |
| PageUp／PageDown | 捲動內容 |
| `/tree` | 查看當輪父子任務、角色和狀態 |
| `/focus ID`／`/focus all` | 查看指定任務的目標、工具紀錄和結果／返回主對話 |
| `/pending` | 查看目前會話的問題與審批 |
| `/answer ID 文字` | 回答指定問題 |
| `/approve ID`／`/deny ID` | 批准／拒絕指定操作 |
| `/status` | 查看模型次數、任務數、網頁次數、已知 token 和期限 |
| Ctrl+C／`/resume` | 暫停／恢復目前會話 |
| `/cancel ID` | 取消指定任務及其未完成子任務 |
| `/cancel` | 取消當輪主任務及其未完成子任務 |
| `/clear` | 清除提示並返回對話；不刪除資料庫歷史 |
| Ctrl+D／`/exit` | 保存會話並離開 |

只有一個待答問題時，直接輸入文字就是回答。多個問題同時存在時，使用 `/answer ID 文字` 指定對象。審批始終需要 `/approve` 或 `/deny`，普通回答不會被解釋成批准。Git 推送審批仍綁定指定任務、分支和完整 commit SHA，不會授予整個會話永久推送權限。

同一輪中的子任務可以並行；同一會話要等當輪結束後才可送出下一輪。等待期間仍能回答問題、處理審批及查看任務。執行中的任務拒絕接收新一輪時，全螢幕介面會保留輸入，方便稍後送出。

串流內容會先標成「草稿」，驗證成功後才成為正式對話結果。工具參數需收齊並通過檢查後才會執行。畫面顯示思考狀態，完整 reasoning 仍留在原任務 snapshot 中，不會成為公開對話歷史。

`/cancel ID` 取消的是任務樹中的一個節點及其後代；同層任務可以繼續。Ctrl+C 是暫停，適合稍後 `/resume`。兩者都不能證明已送出的遠端 API 請求未被處理或未計費。

## 持續對話與會話

`/new [標題]` 建立會話，`/sessions` 列出歷史，`/switch ID` 切換；執行中先按 Ctrl+C 暫停。重新啟動同一資料庫時預設開啟最近會話，也可使用 `--session ID` 或 `--new`。已保存但未完成的工作需明確 `/resume`，開啟歷史不會直接重跑。

下一輪會收到同一會話的使用者訊息、人工澄清和已完成的公開回答。不同會話的私人記憶與對話不混用；子任務取得主 Agent 明確委派的內容，不會自動取得整個會話。`/agent planner`、`/agent researcher`、`/agent coder` 或 `/agent reviewer` 可選擇下一輪主 Agent，執行中的任務保持原身份。

每一輪建立新 workflow 和任務私有工作區。只有正式回答中明確交回的已發布產物，會以校驗後的新 ID 帶入下一輪；未發布檔案和私密同儕訊息不會自動搬過去。歷史和原始操作證據保存在 SQLite；模型讀取受控的網頁預覽，較早網頁片段可換成可重讀的來源引用。其餘對話和 reasoning 不會被靜默刪除；仍超過限制時要求開新會話。

「流程結束」表示執行已收斂，不是系統保證需求達成。終端另列任務和網頁操作的成功／失敗／未知數；有失敗子任務會明確提示核對交付。這份伺服器摘要也會進入後輪上下文，避免模型的一句錯誤診斷掩蓋工具實際成功或失敗的紀錄。

同一資料庫仍只允許一個 CLI 程序持有寫入權。介面開啟時使用內建指令回答和審批；先離開介面，再用外部 `inspect`、`events` 或 `export` 命令讀取該資料庫。SQLite 包含原始對話、reasoning、產物與記憶，應作為私有專案資料保存。

## 每輪上限

預設每輪最多 60 次模型請求、20 個任務、20 次網頁請求，期限 600 秒。委派深度上限為 4，每個任務最多 6 個直接子任務；調度同時執行上限預設 4，可選 1–8。競標、模型重試和子任務共用同輪模型預算；使用者送出的新一輪有獨立預算，所以持續對話不是整個會話的總費用上限。

例如，縮小第一次真實互動的範圍：

```zsh
zsh scripts/terminal.zsh --db data/terminal-live.sqlite \
  --max-calls 12 --max-tasks 6 --max-web-requests 4 \
  --max-depth 2 --max-children 3 --parallelism 2 --deadline 300
```

模型輸出和上下文限制可用 `DEEPSEEK_MAX_OUTPUT_TOKENS`、`DEEPSEEK_MAX_CONTEXT_CHARS` 設定，新的預設分別為 8,192 tokens 和 120,000 字元；thinking 也占輸出上限。`--tool-timeout` 設定 1–120 秒，預設 20 秒，搜尋／抓取和沙箱都使用保存的工作流限制。工作流建立時保存其限制，恢復不會把原先的預算重置；舊工作流的 4,096 token 上限不會因更新而擴大。

這版使用固定四個角色和一組已配置的 DeepSeek 模型。父子委派受到能力、工具和預算限制；目前沒有讓模型任意建立新角色、無限遞迴，或為不同 Agent 自動選擇不同模型。

## 官方網頁搜尋與抓取

真實互動模式預設使用 DeepSeek 官方 Search／Fetch，沿用同一把 DeepSeek API key。researcher 可呼叫 `search_web` 取得標題、URL 和摘要，再用 `fetch_web` 取得指定頁面的 Markdown。其他角色可將需要來源的工作委派給 researcher。

同輪各 Agent 可用 `read_web_result` 分頁讀取已成功保存的公開網頁證據，無需再付一次搜尋／抓取請求；子任務後來失敗也不會讓既有證據消失。相同 provider、相同參數的成功網頁請求在同輪可重用，下一輪不沿用此快取。模型先讀短預覽；需要來源日期、原文或完整網址時再讀保存內容。預覽截斷與 adapter 本身的內容上限會分開標示，完整保留供應商回傳內容也不能證明供應商抽取了來源頁面的每一部分。

- `--web deepseek`：官方搜尋與抓取。
- `--web brave`：沿用 Brave Search，需 `BRAVE_SEARCH_API_KEY`，不提供抓取。
- `--web off`：關閉網頁工具。

也可用 `MULTI_AGENT_WEB_PROVIDER` 指定後端。既有 `run`、`chat`、`evaluate` 為保留配置習慣，仍預設採用可選 Brave；要啟用官方介面，先設定 `MULTI_AGENT_WEB_PROVIDER=deepseek`。

本專案直接呼叫固定的 `https://api.deepseek.com/v1/tools/search` 和 `/v1/tools/fetch`。搜尋詞上限為 128 個 Unicode 字元，結果條數 1–50；搜尋先取摘要，正文按需另行抓取。網頁資料明確標為不可信內容，附來源 URL 和截斷資訊；它們不能變成操作授權。Fetch 拒絕 URL 帳密、本機名稱和私有 IP，由 DeepSeek 端解析一般網域及抓取頁面，本機不直接連線到目標網站。

Search／Fetch 按請求計費，與模型 token 分開。2026-10-19 核對時，官方中文文件標示兩接口各為人民幣 5 元／1,000 次符合條件的成功請求；實際條件與最新價格以[官方說明](https://api-docs.deepseek.com/zh-cn/guides/search/)為準。其他產品 server 端工具的暫時優惠，不視為本專案直接呼叫的免費額度。

狀態列的「網頁次數」是已預留的嘗試數，不是帳單：失敗或結果未知的嘗試也占用本輪上限，避免重試突破限制；本地逾時無法判定遠端最終結果。工具不會自動重試，模型自行再發起搜尋仍需占用預算。模型 token 只累計 API 已回報的用量，缺失部分會標示未知；畫面沒有推算總金額。

## 驗證範圍

功能以離線契約測試、串流傳輸 fixture、會話隔離與恢復測試、終端交互檢查及真實 API 案例驗證。離線示範能檢查介面和狀態流程，不能證明模型會合理分工，也不能評估搜尋內容品質。

新版已使用真實模型、SSE、官方 Search／Fetch 與 Docker，並以自然語言新聞任務重測大內容、證據接續和來源核對。完整結果與失敗紀錄見 [真實協作檢查](LIVE_COLLABORATION_2026-10-19.md)。這是有限案例的工程與內容檢查，尚未建立一般任務成功率；中文輸入法、長時間操作和真實外部 Git 推送仍未驗證。

介面設計參考 Qwen Code 的輸入／串流／子 Agent 事件分工，以及 DeepSeek Harness 的父子層級、完成狀態和恢復深度約束。實作沿用本專案的 Python 核心，沒有複製它們的 TypeScript 介面或引入另一套 Agent loop。固定來源見 [SOURCES.md](SOURCES.md)。
