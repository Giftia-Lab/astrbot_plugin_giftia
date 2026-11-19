## [v0.6.0] - 待发布

### Features

- 新增「子 Agent」页签，支持独立的模型供应商顺序。可将耗时较长的查询工具委托给子agent，避免主人格回复阻塞，子agent的工具循环与主agent次数相同。
- 主 Agent 原生与 XML 函数工具调用固定只记录工具名与执行状态，不保存参数与结果；单轮原生工具循环日志按工具名去重折叠为一条。展开委派记录可以查看子任务的完整工具输出，Token 统计按「子 Agent」汇总模型调用。

### Improvements
- 会话状态卡片支持删除。

## 历史更新日志

- [v0.5.x](docs/changelogs/v0.5.x.md)
- [v0.4.x](docs/changelogs/v0.4.x.md)
- [v0.3.x](docs/changelogs/v0.3.x.md)
- [v0.2.x](docs/changelogs/v0.2.x.md)
- [v0.1.x](docs/changelogs/v0.1.x.md)
