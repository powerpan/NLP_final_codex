# 配置與操作

首次輸入 key 和真實測試的順序見 [LIVE_TEST.md](LIVE_TEST.md)。本地檢查使用 `python -m maplatform doctor`；需要確認檔案／程式任務的前置環境時，加上 `--require-docker`。這個命令不會呼叫模型或下載映像。

## 網頁搜尋與抓取

互動終端預設使用 DeepSeek 官方 Search／Fetch，沿用 `DEEPSEEK_API_KEY`，researcher 可使用 `search_web` 和 `fetch_web`。用 `shell --web off` 關閉，或 `shell --web brave` 換成 Brave。既有批次命令保留可選 Brave 預設；要啟用官方介面，設定 `MULTI_AGENT_WEB_PROVIDER=deepseek`。端點、請求上限、費用口徑與資料處理見 [互動終端指南](TERMINAL.md#官方網頁搜尋與抓取)。

使用 Brave 時，提供 Search API 金鑰即可啟用 `search_web`：

```bash
export BRAVE_SEARCH_API_KEY='自己的搜尋金鑰'
```

工具固定使用 Brave 官方 Web Search endpoint。模型只可傳查詢與條數，不能替換 endpoint。回傳標題、URL 與摘要；不自動抓取結果頁，也不會把搜尋片段當作指令。金鑰只在宿主 adapter 中使用。

## 可選 Git

明確指定可信的本地基準倉庫。程式會在倉庫之外建立任務私有 staging clone，不修改基準工作區。

```bash
export MULTI_AGENT_GIT_REPO='/absolute/path/to/trusted-baseline-repo'
export MULTI_AGENT_GIT_STAGING='/absolute/path/to/private-staging'
export MULTI_AGENT_GIT_REMOTE='ssh://git@your-host/your-repository.git'
```

REMOTE 可省略，此時不能推送。此範例沒有實際使用你的倉庫；請在自己的測試倉庫先驗證。staging 目錄必須是可信宿主私有目錄，不能掛載給 Agent。Git 憑據使用宿主已配置的機制，不能把憑據寫進 remote URL。程式關閉互動認證與 credential helper；HTTPS 私有倉庫可能無法直接認證，SSH 必須具有可非互動使用的主機／私鑰設定。

`submit_patch` 只接受受限的普通 UTF-8 unified diff。它檢查路徑及檔案模式，拒絕 `.git`、Git 控制檔案、符號連結、submodule、binary patch、rename／copy 以及含空白的路徑。先檢查 patch，再在私有分支 commit，停用 hooks、簽名和 fsmonitor。

`git_push` 必須包含 gateway 回傳的任務分支名稱和完整 commit SHA。審批記錄綁定這組完整參數；執行前再次檢查目前 Attempt、approval、SHA 和乾淨狀態，然後只做普通非 force push。這版沒有把整個基準倉庫自動匯入 Agent 沙箱；Agent 需要透過明確輸入／產物取得修改所需的來源內容。

推送結果不確定時，operation 保持 UNKNOWN。使用其真實 ID 核對：

```bash
python -m maplatform --db data/live.sqlite reconcile OPERATION_ID
```

它只讀取固定遠端 ref，不再推送。只有遠端 SHA 與已批准的 SHA 相符，才將操作記錄為已確認成功。遠端未見、已分歧或無法讀取都保留原狀，不能當成安全重推的證明。

## 匯出產物

`inspect` 的任務結果包含實際 artifact ID：

```bash
python -m maplatform --db data/live.sqlite export ARTIFACT_ID --output output/solution.py
```

匯出前檢查本地使用者的 workflow 所有權與內容雜湊；輸出路徑由可信操作者指定。既有檔案不會被覆寫。產物目前限 UTF-8 文字，每份最多 256 KiB。

## 固定案例評測

```bash
python -m maplatform --db data/evaluation.sqlite evaluate examples/evaluation_cases.json --report output/evaluation.json
```

請使用沒有未結束 workflow 的資料庫。套件在執行前固定 ID 和內容雜湊，模型不會看到外部 assertions。報告包含固定案例分母、各案例的狀態與斷言、模型呼叫目的、已知 token 用量、時間與 trace ID。等待人工的案例標示 blocked，不從分母移除。案例內可公開給 Agent 的 acceptance 和外部 assertions 分開保存。

`run`、`chat`、`evaluate` 支援 `--max-calls`、`--max-tasks`、`--max-web-requests`、`--deadline`，均以單個 workflow 計算；多案例套件及群聊會建立多個 workflow。模型呼叫上限包含競標和重試，網頁上限保守計入失敗及結果未知的已預留嘗試。同 workflow 成功的相同網頁請求可重用，會記錄新 call binding／重用事件，不再發送網路請求；分頁讀取保存證據也不消耗網頁次數。

`--tool-timeout` 設定 1–120 秒，預設 20 秒，會保存於 workflow；DeepSeek Search／Fetch 和沙箱都依這個限制執行。`DEEPSEEK_MAX_OUTPUT_TOKENS` 預設 8,192，`DEEPSEEK_MAX_CONTEXT_CHARS` 預設 120,000，在新工作流建立時保存；恢復後不會因環境變數調高而放大已保存的上限。模型 Provider 當前配置較低時仍會套用較低值。thinking 占輸出預算，截斷回應不會被當成可執行工具呼叫。

評測報告會在任何模型請求前以新檔方式建立，拒絕覆寫資料庫、suite 或既有報告。若中途停止，佔位報告標為 `NOT_FINISHED`，應先查看資料庫狀態。events 匯出也要求使用新路徑。工作流／案例失敗或仍在等待時，執行命令返回退出碼 1。

範例為產物契約測試：它只能證明特定文字與產物符合固定要求，不能證明任意程式功能正確，也不能據此比較模型能力。課程正式評測應替換為官方題目、外部正確性測試與重複實驗。記憶預設沿用同使用者／同 Agent 的已存資料，報告會記錄既有記憶數；要做乾淨對照請使用新資料庫。

## Docker 與資料管理

- 只有 Docker 後端能執行 Agent 程式；沒有宿主 fallback。
- 可用 `MULTI_AGENT_SANDBOX_IMAGE` 指定事先安裝且可信的映像，建議正式展示固定到 image digest。
- 每個 task 的具名卷保留供重啟；一般 cleanup 只清容器。需要操作者在任務不再恢復後明確回收對應卷。
- 具名卷沒有本版能保證的硬磁碟配額。展示機需另做儲存限制，並實測 memory／PID／CPU／timeout／網路及檔案隔離。
- 本地資料庫包含私密訊息、reasoning、產物與記憶。預設 `human:local` 是可信 CLI 身份，不是多人服務的身分認證。
- Python stdlib HTTPS 使用背景執行緒。取消本地等待不保證已送出的模型／搜尋請求停止，遠端結果與費用可能未知。

## 交付前需在你的環境完成

以自己的合法金鑰跑 `probe` 和固定套件；換展示機後重跑 `python tests/manual_sandbox_check.py`；如需使用外部 Git，在專用測試倉庫確認 patch、批准、拒絕、遠端核對；再按教授原始 Rubric 補齊課程指定能力。現有離線與容器測試紀錄不會代替模型和課程實測。
