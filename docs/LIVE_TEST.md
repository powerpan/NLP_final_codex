# 第一次真實調用

本地前置檢查及第一次真實文字協作已在 2026-10-19 完成；首次結果見 [LIVE_RESULT_2026-10-19.md](LIVE_RESULT_2026-10-19.md)，前置驗證見 [VALIDATION.md](VALIDATION.md)。真實檔案交付仍待測試；下面保留可重跑的分步操作。

## 最短操作

在專案目錄的終端執行：

```zsh
zsh scripts/live_smoke.zsh
```

腳本會隱藏輸入 API key，再詢問模型 ID。直接 Enter 使用 `deepseek-flash`，這是 2026-10-19 核對時 [DeepSeek 官方入門範例](https://api-docs.deepseek.com/) 使用的 ID；仍須以帳戶實際權限為準。key 只保存在該程序的環境中，不寫入檔案或 shell 指令歷史，也不用貼進聊天。

執行這條命令就會開始真實、可能計費的調用。腳本依次進行本地配置檢查、最多兩次模型請求的工具往返 probe，以及最多六次模型請求的純文字協作。文字案例的上限包括子任務和重試，工作流期限為 180 秒；若 probe 失敗，腳本會停下，不再執行後續案例。

首次 smoke 預設 thinking 開啟、effort 為 `low`、每次輸出上限 4096 tokens；已設定的同名環境變數優先。這是方便排查接入的有界測試，不是效果評測。thinking 的 reasoning 也占輸出預算；遇到截斷時應先檢查報告，再決定是否提高上限。原 Python Provider 的預設 effort 仍為 `high`。

每次使用新的 `data/live-tests/run-…/` 目錄，保存 `doctor-report.json`、`probe-report.json`、SQLite 和 `text-report.json`；前一步失敗時保留已生成的報告並停止。新的 v2 案例明確說明本專案使用腳本替身做離線測試，要求 planner 真正詢問 reviewer；外部斷言檢查 reviewer REPLY 已完成，且所有工具呼叫恰好為一次 `ask_agent`。單靠 summary 自稱收到回覆不會通過。它不需要 Docker、Brave Search 或 Git。

## 分步執行

若需要自己控制每一步，可在 zsh 終端配置環境。下面的 `read` 不會把 key 作為指令參數保存：

```zsh
read -rs 'DEEPSEEK_API_KEY?DeepSeek API key: '
print
export DEEPSEEK_API_KEY
export DEEPSEEK_MODEL='deepseek-flash'
export DEEPSEEK_BASE_URL='https://api.deepseek.com'
export DEEPSEEK_REASONING_EFFORT='low'
export DEEPSEEK_MAX_OUTPUT_TOKENS='4096'

python3 -m maplatform doctor --require-live
python3 -m maplatform probe --report output/probe-live-01.json
python3 -m maplatform --db data/text-live-01.sqlite evaluate examples/text_smoke.json \
  --report output/text-live-01.json --max-calls 6 --max-tasks 2 --deadline 180
```

`doctor` 只檢查本地配置，不能證明 key 有效、帳戶有餘額或模型可用。`probe` 才會連線；`native_tool_roundtrip: true` 表示這兩次工具往返成功，`reasoning_roundtrip` 另行表示是否實際回傳並保留了非空 reasoning。

probe 報告會在請求前以新檔建立；`status` 區分 PASS、FAIL、INTERRUPTED 和未完成。`calls_attempted` 是嘗試次數，`usage` 按次序保留已知 tokens，缺失用量為 `null`，`usage_complete` 為 false。這份報告與文字工作流報告分開，兩者都要查看，不能只把文字報告的用量當整段腳本總量。

## 再測檔案交付

先確認 Docker Desktop 正在運行。此機已下載 `python:3.12-slim` 並完成實際容器 smoke；如果換了展示機或映像，應重跑：

```zsh
python3 -m maplatform doctor --require-live --require-docker
python3 tests/manual_sandbox_check.py
```

手動檢查不呼叫模型，只建立獨立的測試容器與卷，結束後移除這次建立的資源。檢查包括檔案持久化、unittest、非 root、唯讀根目錄、網路不可達、資源限制，以及逾時和輸出超限後的清理。

接著執行原有兩個檔案案例：

```zsh
python3 -m maplatform --db data/artifacts-live-01.sqlite evaluate examples/evaluation_cases.json \
  --report output/artifacts-live-01.json --max-calls 12 --max-tasks 4 --deadline 300
```

這裡是每個案例最多 12 次，兩個案例合計最多 24 次。檢查固定檔案內容、雜湊和交付，不代表任意程式功能正確。正式任務的工作區卷會保留供恢復；不要在任務尚需恢復時刪除。

## 看結果和處理失敗

CLI 退出碼 `0` 表示該次操作成功，`1` 表示 probe／評測失敗或工作流尚未完成，`2` 表示配置或命令錯誤，`130` 表示人工中斷。`WAITING` 需要人類回應，不能當成已完成。`run`、`chat`、`demo`、`evaluate` 不會在有未完成工作的資料庫裡新增一次執行；繼續既有工作請明確使用 `resume`。

報告的 `summary.passed/total` 是固定案例結果；`call_count` 包含競標和重試；`token_usage.reported_totals` 只累計 Provider 確實回報的 tokens。`calls_reporting` 少於 `total_calls` 時，缺失部分是未知用量，不能當成免費。報告不估算貨幣費用，實際帳單以 Provider 為準。

報告和 events 匯出拒絕覆寫既有檔案；重跑請換新檔名。若中斷時報告仍是 `NOT_FINISHED`，先檢查原資料庫，避免重新發起整套測試而重複計費。

```zsh
python3 -m maplatform --db data/text-live-01.sqlite inspect
python3 -m maplatform --db data/text-live-01.sqlite pending
python3 -m maplatform --db data/text-live-01.sqlite events --output output/text-live-01-events.jsonl
```

401 表示認證失敗、402 表示餘額不足、404 表示 endpoint 或模型 ID 需要核對，422 表示參數不被接受。這些訊息不輸出 key 或服務回應本文。[官方錯誤碼](https://api-docs.deepseek.com/quick_start/error_codes/)

`PROVIDER_TRUNCATED` 不會執行不完整的工具呼叫。輸出上限包含 reasoning，過低可能截斷；是否提高需按這次已知用量判斷。工作流保存的上限在恢復後仍生效，單改環境變數不會放大舊工作流預算。[Chat Completions 參數](https://api-docs.deepseek.com/api/create-chat-completion/)

取消或網路逾時不保證遠端停止計費。人工取消中的請求記為 `UNKNOWN`；確認錯誤後再決定是否恢復。不要把含私密對話及 reasoning 的整個 SQLite 放進公開回報。完成終端測試後可執行 `unset DEEPSEEK_API_KEY` 清除該終端環境中的 key。
