## [v0.6.1] - 待定

### Features

- 新增防误判提示词，可在webui -> 模型状态 进行配置，为模型单独启用提示词注入，提示词会注入到用户提示词末尾，可用于防止类似于谷歌安全拦截误判的请求（可能有用）。

### Improvements
- 简化日志输出，并增加展示模型请求耗时。

## [v0.6.0] - 2026-11-20

### Features

- 新增「子 Agent」页签，支持独立的模型供应商顺序。可将耗时较长的查询工具委托给子agent，避免主人格回复阻塞，子agent的工具循环与主agent次数相同。
- 主 Agent 原生与 XML 函数工具调用固定只记录工具名与执行状态，不保存参数与结果；单轮原生工具循环日志按工具名去重折叠为一条。展开委派记录可以查看子任务的完整工具输出，Token 统计按「子 Agent」汇总模型调用。
- webui 长期记忆页签 新增lancedb版本管理，如果完全检索记忆召回卡顿（决策回复后出现较长延迟才回复）可尝试升级lancedb版本，切换版本后根据提示重启Astrbot。

### Improvements
- 会话状态卡片支持删除。
- 优化构建回复提示词内容性能问题。

## 历史更新日志

- [v0.5.x](docs/changelogs/v0.5.x.md)
- [v0.4.x](docs/changelogs/v0.4.x.md)
- [v0.3.x](docs/changelogs/v0.3.x.md)
- [v0.2.x](docs/changelogs/v0.2.x.md)
- [v0.1.x](docs/changelogs/v0.1.x.md)
