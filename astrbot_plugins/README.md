# astrbot_plugins · 自用参考实现（与 CE 本体无关）

这里的 8 个插件跑在 AstrBot 里，通过 HTTP 与 CE 协作。
每个插件只做一件事，配置走环境变量或同目录 `config.json`。

| 插件 | 干什么 | 依赖 |
|---|---|---|
| `mindbeat` | 定时判断"要不要主动开口"（五道闸门：每日上限/深夜静默/冷却/话题重复/空闲） | AstrBot + 本地小模型 |
| `self_edit` | 写自我笔记（需出处、限额 800 字、可遗忘、留历史） | AstrBot + 一个可查的记录源 |
| `sticker` | 按意思挑表情包（不随机） | AstrBot + 嵌入服务 |
| `owner_power` | 主人权限（危险命令拦截 + 重复动作熔断 + 审计） | AstrBot + QQ |
| `local_agent` | 把跑腿活转给 CE | AstrBot + CE |
| `obsidian` | 笔记库读写六工具 | AstrBot（CE 本体已内置 `[搜笔:]` 等标记） |
| `obs_ctl` | 连 OBS Studio（没装就休眠） | AstrBot + OBS |

## 用之前要知道

- **绑定特定部署**：路径/端口/平台都是我们环境的默认值，已改为环境变量
  （`ASTRBOT_DATA` / `PERSONA` / `OWNER_UINS` / `WATCH_DIRS` / `CE_BASE` / `STICKER_DIR`），
  不配也能启动，但功能会降级并给可读提示。
- **涉及产品取向**：要不要让 AI 主动开口、要不要写自我笔记——
  这是**你的决定**，不该由一个通用执行器替你定。
- **改坏了不影响 CE**：这些插件崩了，CE 照跑。

当参考实现看可以；想直接用，请按自己的场景改。
