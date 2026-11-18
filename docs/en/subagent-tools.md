# Delegating tools to a subagent

Open **机器人配置 → 编辑机器人** in the dashboard. Choose an AstrBot persona under **身份与适配器**, then open **子 Agent**. Select the tools to give the subagent and save. The available choices follow the current persona and enabled function tools. Each selected tool moves to the subagent. Leaving the tool list empty disables delegation and omits its instructions from the formal reply system prompt.

The worker tries the **子 Agent 模型供应商** list from top to bottom. If you leave it empty, the worker inherits the providers and order from the formal reply that delegates the task.

![Subagent providers and tools in the bot editor](../images/subagent-tools.jpg)

The main persona can dispatch a task and acknowledge the user in one XML response. Put the following actions after the usual `<status>` block.

```xml
<tool_call name="delegate_task">{"instruction":"Check supported versions and include release-note links."}</tool_call>
<message>I'll check the release notes.</message>
```

Other messages in the same chat can be handled while the task runs. Active tasks appear in the formal reply context with their IDs, original messages, instructions, states and remaining turns. To add a requirement, send `delegate_task` with the existing `task_id` and a new `instruction`.

The worker uses the parent reply's function-calling mode. Its tool-round budget uses the same `max_loop` setting. A new instruction restores the full budget. An in-flight tool finishes and its output is available on the next model turn. Duplicate submissions of the same instruction from the same input batch share a receipt. At the limit, the worker can summarize the evidence it has gathered.

The main persona claims a versioned report under the original chat's reply lock. A follow-up accepted before that claim supersedes the queued notification. After a report is claimed, a later follow-up can continue the same transcript. Tool and model failures are reported to the parent. Restarting the plugin marks unfinished deliveries as interrupted and preserves their records.

In **聊天历史**, expand the task receipt to inspect its instructions, arguments, full tool outputs, errors and reports by revision. The parent receives a summary within the conversation's configured per-message length. Clearing a chat's history also removes its delegated transcripts. Worker model calls appear under **子 Agent** in Token statistics.

Under **身份与适配器 → 主 Agent · 仅记录调用的工具**, select main-agent tools whose operation logs should keep only the tool name, time, and execution status. This applies to native and XML calls. The main agent still receives the result within the current reply. The default is a full operation log.

![Main-agent tool history selection in the bot editor](../images/main-tool-history.jpg)
