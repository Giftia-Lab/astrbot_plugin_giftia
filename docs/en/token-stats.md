# Token statistics

Open **Token统计 (Token statistics)** in the WebUI and select a bot, session and time range. Switch between **按会话 (Session)**, **按模型 (Model)** and **按类型 (Operation type)** to view usage.

The breakdown below the chart shows each item's token usage, share of tokens and call count. Counts follow the same filters as usage and combine current logs with archived statistics. Items with calls and zero tokens still show their counts. TTS characters are tracked separately and excluded from these token call counts.

**Embedding and rerank calls** are included in the call counts. **Model** shows the full model name, operation, local or remote execution, and the provider ID for remote models. **Operation type** shows embedding and rerank totals with local and remote counts. Local means Giftia's built-in FastEmbed; remote means a service invoked through an AstrBot model provider.

Counts represent model invocations initiated by Giftia, including failed attempts. Reranking multiple candidates in one invocation counts once. Empty candidates and skipped operations do not count. Vector indexing, query embedding, and reranking are attributed to the initiating bot and conversation, including cross-conversation retrieval.

AstrBot's current embedding and rerank interfaces do not return token usage to Giftia, so these calls display **Token 未提供 (Token usage not provided)** without a zero-token value or token percentage and do not contribute to token trends. Sessions or totals containing both reported and unreported usage show the reported token sum and a separate count of calls without token usage. Archiving preserves call counts and missing-usage information.

Counts reflect saved data; call counts missing from early archives cannot be reconstructed. Embedding and rerank tracking starts after this update is enabled; earlier calls cannot be reconstructed.

![Example embedding and rerank model details with local and remote call counts (sample data)](../images/token-stats-model-calls.jpg)
