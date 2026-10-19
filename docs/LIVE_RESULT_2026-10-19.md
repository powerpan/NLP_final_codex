# 第一次真實調用結果

2026-10-19 20:50:55–20:50:59（香港時間），我們完成第一次文字協作 smoke。核對對象是 `data/live-tests/run-GSuL19od/text-report.json` 及同目錄的 `text.sqlite`；檢查使用唯讀 SQLite 連線，沒有更改原始報告或工作流。

## 結果

| 項目 | 實際觀察 |
|---|---|
| 模型 | `deepseek-flash` |
| 固定案例 | `text-coordination-smoke-v1`，1/1 通過 |
| 模型請求 | 3 次，全部成功 |
| 用量 | prompt 3,221 + completion 336 = 3,557 tokens |
| 工作流時間 | 約 3.985 秒 |
| 重試／修復 | 0／0 |
| 協作 | planner 詢問 reviewer 一次，reviewer 完成 REPLY，等待解除，planner 恢復一次並交付 |
| 結束狀態 | 無未完成任務、開啟中的等待或審批、未知工具操作 |

報告與資料庫的任務、呼叫帳本、32 筆事件和工具 ID 配對一致。資料庫完整性檢查通過，外鍵違規為 0。原始案例已保留在 `examples/archive/text_smoke_v1.json`，其雜湊與本次報告一致。

這些數字只涵蓋文字工作流。舊腳本在建立結果目錄前執行 probe，因此沒有留下 probe 的原始報告或用量；不能把 3,557 tokens 當成整個啟動腳本的總消耗，也不能補造缺失數據。

## 發現與修正

沒有發現這次執行的狀態機或工具往返故障。不過 reviewer 把「離線測試」理解為固定資料集上的模型評估，planner 也沿用了這個解讀。本專案前置測試使用的是 deterministic scripted responses，不會調用真實模型；原題目未提供這段背景，所以回答不足以證明模型理解了本專案的驗證邊界。

新的 `text-coordination-smoke-v2` 已補上完整背景，並要求簡短回答。外部斷言除了檢查 reviewer 任務完成，現在也會核對整個工作流的實際工具呼叫只能有一次 `ask_agent`。這仍是執行契約檢查，沒有把關鍵字或工具次數當成語義品質評分。

啟動腳本現在先建立結果目錄，再保存 `doctor-report.json` 和 `probe-report.json`。probe 無論成功或失敗，都記錄已嘗試的請求數與已知用量；未知用量保持未知，報告不保存 key 或原始 reasoning。probe 失敗時不會繼續評測，最多 2 + 6 次請求的界線不變。

另修正本地 doctor 未關閉臨時 SQLite 連線造成的 ResourceWarning。這輪修正後，135 項自動測試在 Python 3.12.4／3.14.3 都通過，compileall 和 zsh 語法檢查通過。沒有重新發出付費請求；v2 的真實結果尚未產生。

## 尚未證明的部分

這次成功只證明一次真實文字接入和 Agent 往返。真實模型驅動的檔案交付、程式生成／執行、較複雜任務、長對話與課程 Rubric 仍未驗證。前一輪實際 Docker smoke 是宿主腳本驅動的隔離測試，不能替代模型驅動測試。

目前介面仍是子命令 CLI，結果主要以 JSON 輸出。`chat` 是單次請求，人工等待透過 `pending → answer → resume` 繼續；尚無持續聊天、串流輸出、即時 Agent 面板或同畫面審批。
