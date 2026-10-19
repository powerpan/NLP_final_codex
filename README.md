# Multi-Agent Collaborative Platform

這是一個可執行的課程專案原型：由 DeepSeek 提出工具呼叫與協作動作，Python 控制面負責任務狀態、授權、等待、恢復和驗收。互動終端支援持續對話、串流草稿、父子 Agent 任務樹，以及同一畫面的人工問答和審批。使用 Python 3.11 以上版本；核心和行式入口使用標準庫，完整終端介面另需可選套件 `prompt_toolkit`。

本次以提供的 v0.3 架構為起點，核對 DeepSeek Harness 官方開源實作後調整。原文件屬於設計候選；這個交付也不能代替教授的原始 Rubric。具體變更、來源與尚未驗證的部分見 `docs/`。

macOS／Docker 前置檢查已完成，第一次真實文字協作案例也已通過；具體結果與限制見 [真實調用紀錄](docs/LIVE_RESULT_2026-10-19.md)。在終端執行 `zsh scripts/live_smoke.zsh` 可隱藏輸入 key，依次進行工具 probe 和有調用上限的文字協作。完整步驟和結果判讀見 [首次真實調用](docs/LIVE_TEST.md)，前置修正見 [檢查紀錄](docs/PRE_LIVE_REVIEW.md)。

## 互動終端

安裝後先用離線示範體驗：

```zsh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[terminal]'
zsh scripts/terminal.zsh --demo --db data/terminal-demo.sqlite
```

輸入「協作」可看見主 Agent、子任務、孫任務和唯讀諮詢；coder 會提出問題，再進入操作審批。F2 切換各 Agent 詳情，`/focus ID` 查看指定任務，`/cancel ID` 取消該任務及其後代。示範不呼叫 API，也不真的推送 Git。

真實使用時換一個資料庫：

```zsh
zsh scripts/terminal.zsh --db data/terminal-live.sqlite
```

未配置 key 時會隱藏輸入，key 不寫入會話資料庫。互動模式預設 `deepseek-flash`，可用 `DEEPSEEK_MODEL` 改成帳戶可用模型。使用 `--web off` 可關閉預設啟用的 DeepSeek 官方搜尋／抓取。`/new`、`/sessions`、`/switch ID` 管理會話；只有一個待答問題時直接輸入答案，審批使用 `/approve ID` 或 `/deny ID`。模型和網頁請求分別受每輪預算限制。

不帶參數執行 `.venv/bin/python -m maplatform` 也會進入互動模式。完整指令、上下文與產物延續、費用口徑和驗證邊界見 [互動終端指南](docs/TERMINAL.md)。新版串流、多 Agent、官方網頁工具與自然語言新聞任務的實測、失敗原因和修復見 [真實協作檢查](docs/LIVE_COLLABORATION_2026-10-19.md)。

已針對重複上下文與低快取命中率調整請求組織，保留完整工具推理與可讀證據。用量拆解、修改與驗證口徑見 [Token 用量分析](docs/TOKEN_EFFICIENCY_2026-10-19.md)；`scripts/audit_token_usage.py` 可唯讀分析本機執行紀錄，不呼叫模型。

## 先跑離線示範

在解壓後的專案目錄執行：

```bash
python -m unittest discover -s tests -v
python -m maplatform --db data/demo.sqlite demo --auto-answer
```

示範會進行兩個候選者的競標、委派、Agent 私密問答、人工澄清和產物交回。它使用明確標示的 `DemoProvider` 與只能讀寫記憶體檔案的 `MemorySandbox`，**沒有呼叫真實模型，也沒有執行生成的程式碼**。它驗證執行協定，不是模型能力評測。

若要查看人工等待，先執行不帶 `--auto-answer` 的示範，接著查看：

```bash
python -m maplatform --db data/demo.sqlite pending
python -m maplatform --db data/demo.sqlite inspect
```

`demo` 只在沒有未完成工作的資料庫裡建立新示範。`--auto-answer` 只回答這次示範的問題。請使用獨立示範資料庫；一般 `resume` 使用真實 DeepSeek，會拒絕接管未結束的離線 fixture。離線示範的恢復測試由測試套件完成。

## 使用真實 DeepSeek

可使用上面的互動腳本，或在自己的執行環境設定下列變數。請勿把金鑰寫進程式碼、Git 或回報檔案。以下使用 macOS zsh 隱藏輸入：

```zsh
read -rs 'DEEPSEEK_API_KEY?DeepSeek API key: '
print
export DEEPSEEK_API_KEY
export DEEPSEEK_MODEL='填入帳戶實際可用的模型ID'
export DEEPSEEK_BASE_URL='https://api.deepseek.com'
```

以下批次命令需要明確配置模型 ID，避免複製過時範例。可先執行：

```bash
python -m maplatform doctor --require-live
python -m maplatform probe
```

`doctor` 不發送 API 請求，也不建立資料庫。`probe` 消耗最多兩次真實模型呼叫，測試工具與 reasoning 訊息往返；失敗返回非零退出碼。它不會證明所有模型能力可用，也不納入專案任務的評測成績。

涉及檔案或程式執行時，需要可用的 Docker daemon，以及預先下載的 Python 映像：

```bash
docker pull python:3.12-slim
python -m maplatform --db data/live.sqlite run '請協作實作並測試一個 Python 函式，完成後發布 solution.py。' --acceptance examples/acceptance.json
```

程式不會自動下載映像，也不會在 Docker 不可用時改為宿主執行。純文字協作可以不使用沙箱工具。

## 人工回應與恢復

互動終端可在同一畫面回答與審批。以下是保留的批次命令用法：執行器在只剩人工等待時返回命令列，先查看 `pending`，再使用其中的真實 approval ID：

```bash
python -m maplatform --db data/live.sqlite pending
python -m maplatform --db data/live.sqlite answer APPROVAL_ID answer --text '具體回答'
python -m maplatform --db data/live.sqlite answer APPROVAL_ID approve
python -m maplatform --db data/live.sqlite answer APPROVAL_ID deny
python -m maplatform --db data/live.sqlite resume
```

上面三種 `answer` 形式按請求種類選一種使用。澄清必須提供回答；Git 推送審批則批准或拒絕固定的分支及 commit SHA。拒絕會變成工具結果交還 Agent，任務可以繼續。

單一資料庫同時只允許一個 CLI 程序持有寫入權。互動介面內使用其 `/answer`、`/cancel`、`/status` 指令；外部 `answer`、`cancel`、`inspect` 命令應在前一個程序結束後執行。需要中斷正在執行的批次命令時先按 Ctrl-C，之後可以 `resume` 或取消指定 workflow。HTTP 背景執行緒可能直到其網路逾時才完全結束；不能把本地取消解釋為遠端未計費。

```bash
python -m maplatform --db data/live.sqlite cancel WORKFLOW_ID
python -m maplatform --db data/live.sqlite events --workflow WORKFLOW_ID --output trace.jsonl
```

事件匯出不包含完整模型 reasoning；本地 SQLite 的 snapshot 保存完整私密對話與 reasoning，應像專案私有資料一樣保護。

## 私聊與群聊

```bash
python -m maplatform --db data/live.sqlite chat '請解釋這個設計取捨。' --to reviewer
python -m maplatform --db data/live.sqlite chat '分別給我實作與審查建議。' --to coder,reviewer --group
```

群組共用一條有成員 ACL 的時間線，每個被點名的 Agent 擁有獨立、受限的 REPLY 任務。REPLY 不能再委派或追問其他 Agent，只能用唯讀工具；未被點名的 Agent 不會因廣播自行回覆。這組 `chat` 命令仍是單次訊息入口。持續對話請使用 `shell`，其每輪由一個主 Agent 委派及整合其他角色；目前沒有獨立的多輪群聊產品介面或 Web UI。

## Search、Git 與固定案例評測

互動終端預設使用 DeepSeek 官方 Search／Fetch，沿用同一 API key；批次入口可設定 `MULTI_AGENT_WEB_PROVIDER=deepseek` 啟用。Brave Search 與受控 Git 仍可選用，配置見 [操作文件](docs/OPERATIONS.md)。未配置的服務不會出現在模型的工具選單，舊 snapshot 中的相關呼叫也會被拒絕。

評測使用事先固定的外部 case 清單。案例的成功率分母由清單決定，Agent 拆出多少子任務都不會改變分母。範例套件檢查產物契約；不能把它當成完整程式正確性或模型排名測試。

## 專案結構

| 檔案 | 職責 |
|---|---|
| `maplatform/domain.py` | Agent 名冊、能力與工具上限、預算、共用錯誤 |
| `maplatform/store.py` | SQLite、TaskService、等待、審批、恢復、操作預留 |
| `maplatform/runtime.py` | 路由與競標、模型循環、工具配對、調度與驗收 |
| `maplatform/provider.py` | DeepSeek Chat Completions、SSE 串流聚合與明確的測試替身 |
| `maplatform/terminal.py` | 持續對話、任務樹、輸入、問答與審批介面 |
| `maplatform/sessions.py` | 會話、每輪公開歷史和已發布產物延續 |
| `maplatform/web.py` | DeepSeek 官方 Search／Fetch 與有界回應 |
| `maplatform/evidence.py` | 保存網頁證據的受控預覽與本地分頁讀取 |
| `maplatform/tools.py` | 工具 schema、授權、Docker、產物與記憶 |
| `maplatform/comms.py` | 人類私聊／群聊、成員 ACL、回覆訊息 |
| `maplatform/adapters.py` | 可選 Brave Search、宿主側受控 Git |
| `maplatform/evaluation.py` | 固定案例、外部斷言與可檢查的評測報告 |
| `maplatform/preflight.py` | 不調用 API 的本地配置與 Docker 前置檢查 |
| `scripts/live_smoke.zsh` | 手動啟動、隱藏輸入 key 的有界真實調用測試 |
| `scripts/terminal.zsh` | 使用專案虛擬環境啟動互動終端 |
| `tests/` | 契約、恢復、權限與整合測試 |

這版保留單一狀態來源，沒有 Redis、向量資料庫或第二套工作流引擎。框架和模型接入可以替換，任務及權限契約由專案持有。
