# 自定义问答（astrbot_plugin_qa）

- 作者：云晓
- 版本：1.0.0
- 许可证：MIT（详见 [LICENSE](LICENSE)）

AstrBot 插件：自定义问答，关键词自动回复。发送 `/问答` 维护问答库，普通消息命中问题后自动回复对应答案。

## 功能

- `/问答 添加 <问题> = <答案>`：添加问答（默认按会话/群隔离，可配置全局共享）
- `/问答 删 <编号>`：删除指定编号的问答
- `/问答 列表`：查看当前会话的问答列表
- `/问答 清空`：清空当前会话的所有问答
- 普通消息自动回复：命中问题后自动回复对应答案，不打扰 LLM 会话
- 防刷：同会话命中同一问题在冷却时间内不重复回复

## 指令

| 指令 | 说明 |
| --- | --- |
| `/问答 添加 你好 = 你好呀` | 添加问答「你好 → 你好呀」 |
| `/问答 列表` | 列出当前会话问答 |
| `/问答 删 2` | 删除编号为 2 的问答 |
| `/问答 清空` | 清空当前会话问答 |

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `qa_enable` | bool | true | 是否启用自动问答回复（总开关） |
| `qa_match_mode` | string | contains | 匹配模式：`exact` 精确匹配 / `contains` 包含匹配 |
| `qa_min_length` | int | 2 | 问题最短长度，防止过短问题误触发 |
| `qa_reply_mode` | string | random | 多条命中时：`first` 取第一条 / `random` 随机取一条 |
| `qa_cooldown_seconds` | int | 10 | 同会话命中同一问题的回复冷却间隔（秒） |
| `qa_admin_only_manage` | bool | true | 添加/删除/清空是否仅管理员（`event.role == "admin"` 或 `admin_umos` 白名单） |
| `admin_umos` | string | 空 | 管理员会话 UMO 白名单，英文逗号分隔 |
| `qa_global_shared` | bool | false | 是否全局共享同一份问答库（默认按会话/群隔离） |
| `qa_max_per_session` | int | 100 | 每个会话的最大问答条数，超限拒绝添加 |

## 数据持久化

问答数据保存于 AstrBot 的 `plugin_data/astrbot_plugin_qa/qa_data.json`。
数据文件损坏或格式异常时会自动重置为空库，不影响插件运行。

## 兼容性

- 消息监听同时兼容使用 `filter.on_message` 的旧版 AstrBot 与使用事件类型过滤的新版 AstrBot。
- 只依赖 Python 标准库与 AstrBot 自带组件，无第三方依赖。
