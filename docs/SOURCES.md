# 核對來源

核對日期：2026-10-19。DeepSeek Harness 參考提交固定為 `687f282e8625c6b8d11ff58211a90524be2e4317`（2026-10-17 的發布提交，`0.2.1-rc.2`）；本輪互動終端另核對 Qwen Code 的 `fdb51f8961792033254af91d753bdf62e228bd32`。來源的設計與本專案的實作結果分開判讀，沒有把官方框架的能力直接算成本專案已完成的功能。

## 官方 Harness

| 來源 | 本次用途 |
|---|---|
| [官方倉庫與 README](https://github.com/deepseek-ai/deepseek-harness) | 插件架構、開發者預覽狀態、MIT 授權 |
| [整體架構](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/architecture.md) | 檢查各服務責任邊界 |
| [Agent loop 契約](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/README.md) | 工具配對、取消與未知結果的恢復語義 |
| [工具調度](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/tool-calls.ts) | 工具並發和取消的責任；本版工具 batch 採序列處理 |
| [持久化](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/persistence.md) | 單寫入者和中斷恢復；未移植其日誌格式 |
| [上下文壓縮](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/compaction.md) | 壓縮不能拆壞 call/result；本版先採有界停止 |
| [DeepSeek adapter](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-deepseek/README.md) | 確認 Messages 與本版 Chat Completions 的協定差異 |
| [Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/README.md) | 確認其為子程序控制介面，未假設是原生 Python loop |
| [安全說明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/SAFETY.md) | 插件模組化不等於不可信程式隔離 |

## 互動終端與父子 Agent

本輪讀取以下固定版本原始碼，借鑑狀態呈現和責任分工。Python 實作仍由本專案的 Runtime、Store 和 SessionService 管理，沒有複製上游 TypeScript 程式碼，也沒有接入上游 Agent 執行器。

| 來源 | 理解到的設計與本專案處理 |
|---|---|
| [Qwen Code Composer](https://github.com/QwenLM/qwen-code/blob/fdb51f8961792033254af91d753bdf62e228bd32/packages/cli/src/ui/components/Composer.tsx) | 輸入、串流狀態和確認畫面有各自的呈現責任。本專案使用固定輸入區、暫時草稿和獨立待答區。 |
| [Qwen Code 輸入按鍵分類](https://github.com/QwenLM/qwen-code/blob/fdb51f8961792033254af91d753bdf62e228bd32/packages/cli/src/ui/opentui/input-prompt-key.ts) | 可列印的中文／emoji 與控制鍵需要區分。本專案將輸入交給 `prompt_toolkit`，輸出另清除終端控制字元。未移植其鍵盤協定實作。 |
| [Qwen Code SubAgentTracker](https://github.com/QwenLM/qwen-code/blob/fdb51f8961792033254af91d753bdf62e228bd32/packages/cli/src/acp-integration/session/SubAgentTracker.ts) | 子 Agent 工具開始、結果、串流、用量和審批需帶父子關聯。本專案依原有 task／operation ID 呈現，審批仍寫入原有等待紀錄。 |
| [Qwen Code SubagentExecutor 契約](https://github.com/QwenLM/qwen-code/blob/fdb51f8961792033254af91d753bdf62e228bd32/packages/core/src/agents/runtime/subagent-executor.ts) | 執行、取消訊號、最終結果和資源釋放屬於不同責任。這裡只作邊界參考；本專案沒有實作其外部執行器或混合模型能力。 |
| [DeepSeek Harness 父子導航](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/client/ui-subagent/src/client/SubagentHeaderLineage.tsx) | 保留父子層級及子任務狀態，讓使用者能進入個別任務。本專案提供任務樹和 `/focus`。 |
| [DeepSeek Harness 深度計算](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/subagent/subagent/src/depth.ts) | 恢復不能把子任務當根節點重算深度。本專案沿用持久化深度及 workflow 預算，不因切換畫面重置。 |
| [DeepSeek Harness 子任務生命週期](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/subagent/subagent/src/lifecycle.ts) | 資源已釋放不代表工作成功。本專案分別呈現完成、失敗、取消、逾時及等待。 |
| [DeepSeek Harness 結構化結果](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/subagent/subagent/src/structured.ts) | 暫時生成內容需經正式結果邊界才可算完成。本專案只把驗證後的根任務公開回答投影到會話歷史；串流草稿不提前成為成功結果。 |

以上來源分別保有其授權；Qwen Code 檔案標示 Apache-2.0，DeepSeek Harness 倉庫採 MIT。參考設計不等於宣稱與 Codex CLI、Qwen Code 或 DeepSeek Harness 功能相容。

## 官方 DeepSeek API

- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)：tools 請求回傳歷史 reasoning 的規則、effort 控制。
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)：模型只提出呼叫，工具由應用執行；本地仍需檢查參數。
- [JSON Output](https://api-docs.deepseek.com/guides/json_mode/)：Agent／Bid／格式修復使用 `response_format={"type":"json_object"}`，提示含格式範例；仍需本地嚴格驗收，不能把 JSON 語法有效當作內容正確。已用兩次真實 SSE／原生工具往返核對。
- [List Models](https://api-docs.deepseek.com/api/list-models/)：帳戶可用模型與能力應以查詢及實測為準。
- [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)：本輪核對輸出上限、截斷和資源不足／中斷的停止原因。
- [Chat Completions 串流](https://api-docs.deepseek.com/api/create-chat-completion/)：串流以 SSE delta 聚合內容、reasoning 與工具呼叫，收齊後仍需通過完整回應和工具參數驗證。
- [Search API](https://api-docs.deepseek.com/api/search/)：官方搜尋 endpoint、Unicode 查詢長度、結果條數及摘要欄位。本專案使用 `need_content=false`，正文按需另抓。
- [Fetch API](https://api-docs.deepseek.com/api/fetch/)：官方抓取 endpoint、URL 限制、標題及 Markdown 正文。
- [Search／Fetch 指南](https://api-docs.deepseek.com/zh-cn/guides/search/)：Bearer 鑑權、錯誤狀態和按請求計費。畫面記錄嘗試數及已知模型用量，不把本地計數視為遠端帳單，也不套用其他產品的暫時優惠。
- [Error Codes](https://api-docs.deepseek.com/quick_start/error_codes/)：本輪核對認證、餘額和請求參數錯誤的診斷文字。
- [Rate Limit & Isolation](https://api-docs.deepseek.com/quick_start/rate_limit/)：非串流 keep-alive 空行；HTTP 讀取需要總期限，不能只依賴每次 socket read 的 timeout。
- [官方入門範例](https://api-docs.deepseek.com/)：本輪互動 smoke 腳本的可更換模型預設 `deepseek-flash`；沒有把文件列出模型等同帳戶已獲授權。

官方文件的 API 能力不等於課程金鑰具有權限。`probe` 需要使用自己的合法憑據後，才會產生實測結果。

新版互動終端核心、SSE、Search／Fetch 已另做真實調用，並保留失敗輪次、原因和修復結果；見 [真實協作與新聞檢查](LIVE_COLLABORATION_2026-10-19.md)。程式驅動測試不代替真人輸入法或長時間終端體驗。

終端 Markdown 解析使用 [markdown-it-py 官方文件](https://markdown-it-py.readthedocs.io/en/latest/using.html)描述的 token 介面，轉為 `prompt_toolkit` 樣式片段。HTML 關閉，圖片與連結只顯示文字；程式碼和跳脫字元交由解析器保留。

## 使用者提供的設計

`Multi-Agent_核心架构详细设计_v0.3_2026-10-14.md`。它是本次的需求和設計參考，明確標示原始 Rubric 未獨立核實。本交付沒有覆寫原檔。
