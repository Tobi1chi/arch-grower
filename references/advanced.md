# advanced 模式手册

以下 `arch` 命令按 [CLI 入口](../SKILL.md#cli-入口) 执行。

## 1. 两种模式

| | basic（新项目默认） | advanced |
| :--- | :--- | :--- |
| 拓扑 | 只有一条流水线 `pipeline` | 另有星形中枢 `star`、环形闭环 `ring`，可分层嵌套子系统 |
| 预设 | `standard` / `lean` | 另有 `agent`（默认环形）、`platform`（默认星形） |
| 命令 | 推演、储备区、连线与契约、现实验证（`verify` / `open` / `facts` / `ground`）、实现与回滚 | 另有 `fold` / `promote` / `unfold`、`graveyard add`、`rescope`、`legacy`、`migrate-records` |
| MCP | 只列出 basic 工具，并隐藏子系统参数（`scope`）与星形角色 | 列出全部工具 |

- `init --topology star|ring` 或 `--preset agent|platform` 时，项目直接按 advanced 初始化。
- 引入模式之前创建的项目不需要手动设置：用了星形 / 环形拓扑、子系统、`agent` / `platform` 预设，或还有旧格式记录待迁移的，自动按 advanced 处理；其余按 basic 处理。
- basic 模式下调用高级命令会返回 `[ADVANCED_MODE_REQUIRED]`，图不会有任何改动。

## 2. 什么时候切到 advanced

最小路径优先。只有满足以下任一条件，且用户同意后，才运行 `arch config --mode advanced`（MCP：`arch_config mode=advanced`）：

1. 系统本质上不是一条流水线：有一个中心总线 / 路由器被多个模块围绕（星形），或者每轮"感知 → 决策 → 执行"要回到起点（环形）。
2. 链长、实心节点数等护栏告警反复出现，需要用 `fold` 把稳定的子图折叠成模块。
3. 某个节点内部本身就是一套独立拓扑，需要用 `promote` 升级为子系统，在 `--in` 作用域里单独推演。
4. 旧项目需要迁移旧格式记录，或 `global` 事实超出预算、需要用 `rescope` 收窄。

切换后 MCP 服务会通知客户端刷新工具列表。只要还没用到 advanced 专属的能力，可以用 `arch config --mode basic` 切回。

## 3. 多拓扑与子系统

拓扑选型、角色（`entry` / `exit` / `hub` / `spoke`）、回流边与双向边的建模见 [topology_patterns.md](topology_patterns.md)。

- **自底向上折叠**：活跃实心节点或主干链长超过 `preset` 提醒线时，挑一组已确定、彼此连通的节点调用 `arch_fold`（可附 `--topology`），折叠为一个超级节点。需要把子系统拆回父层时用 `arch_unfold`。
- **自顶向下升级子系统**：某个节点内部天然是独立拓扑时（例如流水线里的一个环形 ReAct 引擎，或一个星形插件中枢），调用 `arch_promote --node "$NODE_ID" --topology ring|star|pipeline`，把它升级为子系统超级节点。之后带上 `--in "$NODE_ID"` 在子系统内部推演，父层不用 `unfold`。
  - 嵌套深度受 `preset.max_depth`（默认 2）保护。
  - 只能升级还没开始实现的节点；已经实现的节点先 `reset-node`。
- **护栏**：`max_depth`、`max_chain_len`、`max_hub_degree`、`max_cycle_len` 都在 `.arch/config.json` 里，可用 `arch config` 查看，经用户确认后调整。

## 4. 记录维护

| 场景 | 命令 |
| :--- | :--- |
| 补记历史上的废弃方案（不重置节点） | `arch graveyard add --node --scheme --reason` |
| `global` 事实超出预算（`[GLOBAL_FACTS_OVER_BUDGET]`） | `arch rescope --id GT-xx --scope <节点>`，把只与部分节点相关的事实收窄。预算用 `arch config --global-fact-limit` 调整 |
| 待办项的作用域写错了 | `arch rescope --id OI-xx --scope <节点/阶段>` |

## 5. 迁移旧项目

`focus` 返回 `[RECORDS_NOT_MIGRATED]` 时，项目的事实与墓志仍是旧格式，每次都全文拼接。征得用户同意后运行 `arch migrate-records`，再按下面的步骤处理迁移结果。

`arch migrate-records` 不解析旧 `ground_truth.md` / `graveyard.md` 的内容，而是把它们原样切成小块，存为待处理的迁移原文（`legacy`）：

- 每个列表条目连同缩进的续行是一块。
- 其余段落（状态表、长段叙述、代码块）按所在标题各成一块。
- 每块记录来源文件和标题路径。

迁移前会自动打影子快照，重复运行不会重复导入。迁移后 `status` / `focus` 会提示 `[LEGACY_RECORDS]`，此时逐条处理，每次只看一块：

1. `arch legacy show` 显示下一条待处理原文（也可 `--id LG-xx`）。
2. 判断类型，用对应命令记录（一块可以拆成多条）：
   - 决策与硬约束 → `arch verify --fact --constraint --scope <节点/阶段>`；只在约束对所有节点都成立时用 `global`。原文推翻了已有事实时加 `--supersedes GT-xx`。
   - 废弃方案 → `arch graveyard add --node --scheme --reason`。
   - 待实测 / 待调研 / 待决策 → `arch open add --question --kind --owner --scope [--blocking]`。
   - 实现状态与验证程度 → `arch set-node-state --node --impl --verify [--verify-date]`；参考文档用 `--add-ref`，实现文件用 `--file-path`。
   - 流程规则、发布运维 → 移到项目的 AGENTS.md，不进 arch 记录。
   - 调查过程 → 只把结论写成事实，用 `--source` 指向证据路径。
   - 已过时、重复或无用的内容 → 不记录。
3. `arch legacy resolve --id LG-xx --note "拆为 GT-12、OI-03"`。没有记录任何内容时，在 note 里写明原因（如"与 GT-04 重复""流程规则，已移到 AGENTS.md"）。
4. `arch legacy list` 为空，才算迁移完成。

改写用户确认过的约束时保持原意，不要增减条件；拿不准时按原文照录，或问用户。原文一直保存在 `records.json` 里（可用 `arch legacy list --all`、`arch legacy show --id LG-xx` 对照），处理完之前也会继续显示在生成的 md 里。
