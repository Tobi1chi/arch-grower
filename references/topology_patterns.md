# 多拓扑建模、分层子系统与项目 Preset 指南

真实软件系统往往不是单一线性流水线，而是**“宏观一种拓扑 + 局部子系统各自采用最合适的拓扑”**。本指南说明何时选择 `pipeline` / `star` / `ring`，如何用 `super_node` 封装子系统，以及如何用项目 `preset` 控制架构复杂度。

---

## 一、 三种一等拓扑与适用场景

| 拓扑 (`topology`) | 典型场景 | 聚焦推进顺序 | Walking Skeleton 贯通条件 |
| :--- | :--- | :--- | :--- |
| **`pipeline`**（流水线，默认） | ETL、音视频处理、请求-响应链路、编译管线 | 最上游待定节点优先（Kahn 拓扑序，忽略 `feedback` 边） | 忽略 `feedback` 边后，存在一条 `入口 → ... → 出口` 的全实心通路 |
| **`star`**（星形 / 中枢辐射） | 事件总线、插件宿主、微内核、多通道路由中心 | **强制先敲定中心 `hub`**，再逐个收敛外围 `spoke` | 中心 `hub` 实心 + 至少 2 个相连 `spoke` 实心 |
| **`ring`**（环形 / 反馈闭环） | Agent ReAct 循环、控制回路、记忆巩固闭环、状态机 | 从 `entry` 锚点出发沿主线逐站敲定 | 存在一条长度 ≥ 2 的全实心有向闭环 |

### 边语义 (`--kind`)
- `primary`（默认）：主数据流 / 主控制流。
- `feedback`：闭环回流、状态写回、审批打断或重试回路。在 `pipeline` 中允许用 `arch connect --kind feedback` 补充闭环反馈，而不破坏主干骨架检测与泳道排布。
- `bidirectional`：双向通信（RPC / 发布-订阅 / 中枢与插件双向交互），常用于 `star` 拓扑。

### 专属架构权衡维度
- **推演 `ring` 环路时**，二选一权衡应覆盖：**终止条件与防死循环**、**回写一致性（同步阻塞 vs 异步缓冲）**、**异常熔断策略**。
- **推演 `star` 中枢时**，二选一权衡应覆盖：**通信模型（同步 RPC vs 异步事件总线）**、**胖中枢（集中编排）vs 瘦总线（仅路由分发）**、**外围模块故障隔离边界**。

---

## 二、 子系统化 `super_node`（容器级拓扑）

`topology` 不仅属于顶层图，更属于**每一个 `super_node` 容器**：
- **宏观流水线 + 微观星形/环形**：外层用 `pipeline` 保持端到端清晰，某个节点（如 `reasoning_engine`）通过 `promote` 升级为 `ring` 子系统，另一个节点（如 `router_hub`）升级为 `star` 子系统。
- **宏观星形 + 微观流水线**：外层用 `star` 表达中心总线，每个 `spoke` 升级为内部带 `pipeline` 的子系统。

### 两种创建方式
1. **自顶向下（推荐先搭骨架再下钻）**：
   ```bash
   arch promote --node reasoning_engine --topology ring
   arch focus --in reasoning_engine
   arch collapse --in reasoning_engine --node reasoning_engine_perceive ...
   ```
   外层连线自动作为子系统的**只读边界端口**保留；在子系统内推演不需要 `unfold`，不会污染父层节点上限和布局。
2. **自底向上（整理已确定子图）**：
   ```bash
   arch fold --nodes "a,b,c" --id router_cluster --label "路由中枢" --spec "..." --topology star
   ```

### 组合式 Walking Skeleton
当父层包含异构拓扑的子系统（`ring` 或 `star`）时，引擎不会将其暴力拍平，而是**先按子系统自己的拓扑判定它是否贯通**；只有子系统内部贯通后，它在父层才算就绪节点，彻底杜绝假阳性骨架提示。

---

## 三、 项目 Preset 护栏（`.arch/config.json`）

每个项目初始化时都会写入 `.arch/config.json`，提供“防 AI 过度发散的硬熔断 + 提醒重构的软告警”：

| Preset | `default_topology` | `max_depth` | `max_chain_len` | `max_hub_degree` | `max_cycle_len` | `allow_feedback` | 适用项目 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **`standard`**（默认） | `pipeline` | `2` | `6` | `6` | `5` | `true` | 绝大多数通用应用与服务 |
| **`lean`** | `pipeline` | `1` | `5` | `4` | `4` | `false` | 脚本、工具、极简 MVP（防过度设计） |
| **`agent`** | `ring` | `2` | `6` | `6` | `6` | `true` | AI Agent、带记忆回写/人机审批的闭环系统 |
| **`platform`** | `star` | `3` | `8` | `8` | `6` | `true` | 平台中枢、插件生态、多子系统架构 |

- **硬护栏**：`max_depth`（默认 2，对应顶层 → 子系统 → 孙子模块）、`max_active_holes`、`auto_bridge_limit`（防止对星形中枢误执行 `deactivate` 缝出网状乱边，此时应改用 `reset-node` 或 `--no-bridge`）。
- **软护栏**：`max_chain_len`、`max_hub_degree`、`max_cycle_len`、`max_unfolded_solid`，超限时在 `focus` / `status` / 看板 HUD 给出提醒。
- **按需调整**：经用户确认后可用 `arch config --max-depth 3 --max-chain-len 8` 单项覆盖，或用 `arch config --preset agent` 切换预设。`config.json` 在 `arch fork` 回滚图分支时会被自动保留。
