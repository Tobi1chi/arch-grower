# 图内核重构计划：语义命令不变，内部收敛到 CRUD 内核 + 不变式校验

> 状态：阶段 0 已完成（含 C 类修复）· 2026-11-26 · 分支基线 `feat/multi-topology-subsystems-presets` (d346aa4)

## 1. 背景与结论

有人建议把 arch-grower 抽象成 graph CRUD。结论是：

- **对外接口不改为 CRUD。** `collapse / deactivate / fold / promote / add-node --after --before` 等语义命令是 agent 的 DSL，承载"缝合旁路、拆边继承契约、端口重映射"等机制。暴露裸 CRUD 会把这些机制推回给 LLM，违反"人定策略，机定机制"。
- **对内收敛到一个小的图内核。** 目前每个 op 直接手改嵌套容器的 `nodes/edges/children/inner_edges`，且 `_save_graph`（`common.py:358`）写盘前没有任何校验。最近 6 个 fix commit（`899914b`…`d26b539`）几乎都集中在 `fold_ops.py` 和 `graph.py` 的边界端口、`members` 溯源、`kind` 保留等问题上，说明"安全地改图"的逻辑在各个 op 里被重复实现，而且容易出错。

## 2. 目标 / 非目标

**目标**
1. 写盘前统一执行全图不变式校验。非法状态直接报错，不落盘、不打 shadow 快照。
2. 提供一组图内核原语，供所有语义 op 组合调用。
3. 每个语义 op 变短，fold/unfold/deactivate 不再各自实现改边逻辑。

**非目标**
- 不改变 CLI / MCP 的命令名、参数和输出文案。现有 118 个测试必须原样通过。
- 不改变 `graph.json` 的磁盘格式（不做 schema 迁移）。
- 本计划不包含 `records.json` 的重构。

## 3. 当前图模型（重构必须保持）

| 元素 | 位置 | 说明 |
| :--- | :--- | :--- |
| 容器 | 根 `graph` 用 `nodes`/`edges`；`super_node` 用 `children`/`inner_edges` | `_container_nodes/_container_edges` 已抹平差异 |
| 节点状态 | `hole` / `solid` / `fog` / `super_node` | `SOLID_STATUSES = (solid, super_node)` |
| 节点角色 | `"" / entry / exit / hub / spoke` | `NODE_ROLES` |
| 边 | `{from, to, contract, kind?, members?, auto_contract?}` | `kind ∈ primary/feedback/bidirectional`；`members` 为折叠前原始边 |
| 子系统端口 | `entry_children` / `exit_children` / `parent_ports` | fold 时记录，unfold 时恢复 |
| 拓扑 | 容器级 `topology ∈ pipeline/star/ring` | 缺省 pipeline |
| 步数 | `graph["step"]` | 每次写操作 +1，用于 shadow tag |

## 4. 不变式清单（`validate_graph(graph, cfg) -> List[Violation]`）

按严重程度分两级。**硬错误**阻止写盘；**软告警**沿用现有 `_container_warnings` 输出，不阻止写盘。

### 硬错误（结构完整性）
- **I1 ID 全局唯一**：跨所有嵌套层级，节点 ID 不重复。
- **I2 边端点在本容器内**：每条边的 `from`/`to` 都是同一容器的直接子节点（禁止跨层边）。
- **I3 无自环**：`from != to`。
- **I4 无重复边**：同一容器内 `(from, to)` 至多一条；`bidirectional` 的 `(a,b)` 与 `(b,a)` 视为同一条。
- **I5 枚举合法**：`status`、`role`、`kind`、`topology` 都在常量表内。
- **I6 super_node 形态**：`status == super_node` ⇔ 有 `children` 和 `inner_edges`；非 super_node 不得带这两个字段。
- **I7 端口闭合**：`entry_children`、`exit_children` 中的每个 ID 都是该 super_node 的直接子节点，且列表非空。
- **I9 members 溯源一致**：带 `members` 的边，其每个 member 的 `from`/`to` 都能解析到某个节点：要么是外层边端点本身，要么是该端点（若为 super_node）子树中的节点。`auto_contract` 存在时，`members` 也必须存在。

### 软告警（`warning`，不阻止写盘）
- **I8 parent_ports 可解析**：`parent_ports` 中的 ID 仍在该 super_node 子树内。*降级原因*：在外层子系统内部展开嵌套子系统时，这里本来就会留下失效 ID，`op_unfold` 的 `current_children` 也明确容忍这种情况。
- **I11 嵌套深度**：深度不超过 `cfg.max_depth`。*降级原因*：用户可以事后用 `arch config` 收紧 preset，不能因此锁死所有写操作。
- 链长、活跃空洞数、度数、环长等护栏继续走 `_container_warnings`，不并入不变式。

### 已删除
- ~~I10 fog 隔离~~：`add-node --status fog --after X` 会有意给储备分支保留连线（见 `test_fog_branch_does_not_block_skeleton`），fog 节点带边是设计行为。

> 每条不变式都要能指出违规位置（容器路径 + 节点/边），错误信息沿用 `ArchEngineError` 的 JIT 指令风格，前缀 `[GraphInvariant:I7]`。

## 5. 图内核原语（`arch_engine/kernel.py`）

只负责结构变更，不含业务语义（护栏、提示文案、快照都不在这里）：

```python
class GraphTx:                       # 一次命令一个事务
    def __init__(self, graph, cfg)   # 深拷贝 graph，失败时原对象不受影响
    def container(self, scope) -> Container
    def commit(self, tag, message)   # validate → _save_graph → _shadow_commit → step += 1

class Container:                     # 抹平 root / super_node 差异
    nodes / edges / topology
    def get(node_id) / has(node_id)
    def add_node(node, index=None)
    def remove_node(node_id, *, drop_edges=True) -> removed_edges
    def add_edge(u, v, *, contract="TBD", kind="primary", members=None)
    def remove_edge(u, v) -> edge
    def edges_of(node_id, direction="any", include_feedback=True)
    def replace_endpoint(old_id, new_id)        # 合并 _swap_node_refs 的逻辑
    def split_edge(u, v, mid, *, kind_override="", contract="")   # 来自 _wire_inserted_node
    def contract_into(member_ids, super_node) -> boundary_edges    # fold 的结构部分
    def expand(super_id) -> restored_ids                            # unfold 的结构部分
```

`_edge_members`、`_edge_from_members`、`_merge_contracts` 移入 kernel，作为边溯源的唯一实现。

## 6. 分阶段推进

每个阶段单独提交，阶段结束时全量测试必须绿。

### 阶段 0：不变式检查器先行（≈0.5–1 天）
1. 新建 `arch_engine/invariants.py`，实现 I1–I11。
2. 在 `_save_graph` 中先调用校验。
3. 加 `ARCH_INVARIANTS=warn|strict` 环境开关：测试里用 `strict`，用户侧先用 `warn`（只打印、不阻止），观察一个版本后再默认 `strict`。
4. 跑现有 118 个测试，**记录所有触发的违规**：
   - 违规来自真实 bug → 单独提交修复，并补回归测试。
   - 违规来自不变式定义过严 → 修改不变式，并在本文档记录原因。
5. 新增 `tests/test_invariants.py`：每条不变式至少一个"构造非法图 → 被拒"的用例。

**决策点**：如果阶段 0 几乎抓不到问题，说明现有 op 已经足够稳，阶段 2–4 可以降低优先级，只保留阶段 1。

### 阶段 1：事务与内核骨架（≈1 天）
1. 实现 `GraphTx`，把"深拷贝 → 修改 → 校验 → 写盘 → 快照 → step+1"收拢到一处。目前 45 处 `_save_graph`/`_shadow_commit` 调用各自处理这些步骤。
2. 实现 `Container` 的基础原语（get/add/remove/edges_of/replace_endpoint），把 `_replace_edges`、`_replace_container_lists`、`_swap_node_refs` 改成调用它们。
3. 现有 op 暂时只改"开头 load、结尾 save"两处，改用 `GraphTx`，函数主体先不动。

### 阶段 2：迁移 fold_ops（bug 最多，收益最大）（≈1–2 天）
1. 把 `op_fold` 拆成两部分：
   - **校验/决策**（连通性、凸性 SCC 检查、深度护栏、entry/exit 推断、拓扑推断）留在 `fold_ops.py`。
   - **结构变更**（移动子节点、分组边界边、生成 members、维护 parent_ports）放进 `Container.contract_into`。
2. `op_unfold` 改为对称的 `Container.expand`，补一个性质测试：任意合法子图 `fold → unfold` 后，图与原图结构相等（忽略 step）。
3. `op_promote` 复用 `contract_into` / `_starter_subsystem_skeleton`。

### 阶段 3：迁移 graph_ops（≈1–2 天）
按风险从高到低：`deactivate`（旁路缝合 + 占位节点）→ `add-node`/`activate`（`split_edge`）→ `connect/disconnect/set-edge-contract` → `collapse/reset-node/set-node-state`。

### 阶段 4（可选）：底层逃生口 `arch patch`
仅在阶段 0–3 完成后考虑。接受 JSON Patch 风格的操作列表，在一个 `GraphTx` 中执行，并经过同一套不变式校验。默认只在 advanced 模式开放，SKILL.md 中写明：仅当语义命令无法表达时使用。

### 阶段 0 结果（2026-11-26）

**已落地**
- `scripts/arch_engine/invariants.py`：实现 `validate_graph` / `enforce`；`_save_graph` 写盘前调用（延迟导入，避免循环依赖）。
- `ARCH_INVARIANTS=off|warn|strict`，默认 `warn`，输出到 stderr（MCP 协议走 stdout，所以不能打到 stdout）。`tests/test_arch_cli.py` 默认使用 `strict`。
- `tests/test_invariants.py`：23 个用例，覆盖每条不变式的正反两面、三种模式，以及"strict 下坏图不落盘"。
- `tests/fuzz_ops.py`：随机操作序列模糊测试，可确定性回放（不在 unittest 自动发现范围内）。

**发现**
- 原有 118 个回归测试在 strict 下**零违规**：这些测试只覆盖"规矩"的操作路径。
- 模糊测试（60 seeds × 80 步，fold 成功约 160 次、unfold 约 140 次）**修复前 65 条违规 / 0 崩溃**，全部是真问题，没有一条是不变式定义过严。归为四类根因：

| # | 根因 | 不变式 | 涉及 op | 处理 |
| :--- | :--- | :--- | :--- | :--- |
| A | `activate --after/--before` 指向自己；`--after == --before` | I3 / I4 | activate, add-node | **已修复**：`_wire_inserted_node` 入口校验 |
| B | fog 储备分支本来就带连线，激活时又追加同一条 → 重复边 | I4 | activate | **已修复**：追加前按 `op_connect` 的去重规则检查 |
| C | 双向边 `A<->B` 与反向单向边 `B->A` 并存。`op_connect` 只在"已有边是双向"时拦截反向，"新边是双向、已有反向单向"不拦；fold 合并边界边时也会自然产生这种组合 | I4 | connect, set-edge-contract --kind, fold, activate | **已修复**：选定"合并为一条"（见下） |
| D | 在外层子系统内部展开嵌套子系统后，祖先层边界边的 `members` 仍指向已消失的子系统 ID；之后展开外层时只能走"折叠期间新增连线"分支，丢失溯源 | I9（以及 I8 告警） | unfold | **留给阶段 2**：`Container.expand` 需同时改写祖先边界边的 members，映射规则依赖被展开节点的 entry/exit_children |

- 修复 A、B 后：违规 65 → 23，剩下的全部属于 C 和 D；自环（I3）归零。新增 4 个回归测试（`InsertWiringGuardTest`）。

- 修复 C 后：违规 23 → 12，剩下的全部属于 D；I4 归零。新增 6 个回归测试（`BidirectionalMergeTest`）。

**C 的决策：合并为一条（2026-11-26）**

规则：同一对节点之间只要有一条双向边，这对节点之间就只保留这一条边。其他边（任意方向、任意类型）并入它，原边作为 `members` 保留各自的方向和类型。没有双向边的节点对不受影响（`a -> b` primary 与 `b -> a` feedback 仍是正常的闭环）。

实现：
- `graph._merge_bidirectional_pairs(edges)`：唯一的合并实现，保留第一条双向边的方向和位置。`connect`、`set-edge-contract`、`add-node`/`activate`（`_wire_inserted_node`）、`deactivate` 旁路缝合、`fold`、`unfold` 写回连线前都调用它。这是阶段 1 内核原语的第一个雏形。
- `_edge_from_members`：成员中只要有双向边，合并结果就是双向边（以前混合类型一律记为 primary）。
- `members` 方向：只有双向边允许出现反向成员；I9 同步放宽。`unfold`、`_wire_inserted_node` 改为按成员自身的方向归位，不再按边的方向。
- `_flatten_folds`：合并双向边中没有 `kind` 的成员按 primary 处理，避免把单向流误当作双向，影响 walking skeleton 判定。
- `connect`：任意新边遇到已有双向边都拒绝（双向边已覆盖两个方向）；新的双向边遇到已有单向边则合并，并在输出中说明。
- `set-edge-contract --kind <单向>` 作用于合并后的双向边时，按方向拆回两条边。

**决策点结论**：检查器抓到了真 bug，阶段 1–3 按原计划推进；其中 D 证实 fold/unfold 的溯源维护需要集中到内核。

## 7. 测试策略

- **回归**：现有 `tests/test_arch_cli.py`（118 个用例）每个阶段必须全绿，输出文案不变。
- **不变式单测**：`tests/test_invariants.py`，每条不变式一正一反。
- **往返性质**：fold/unfold、deactivate/activate 往返后结构等价。如有条件，用随机小图（≤8 节点）跑几百次，不引入第三方依赖，自己写一个简单生成器即可。
- **严格模式**：测试 fixture 统一设置 `ARCH_INVARIANTS=strict`，让每个现有用例同时充当不变式的覆盖测试。

## 8. 风险与缓解

| 风险 | 缓解 |
| :--- | :--- |
| 用户已有的 `graph.json` 本身就违反不变式，升级后无法写入 | 阶段 0 默认 `warn`；新增 `arch doctor` 子命令列出违规并给出修复建议 |
| 不变式定义与现有语义不一致（尤其是 I9、I10） | 先用测试反推现有行为，再写不变式；有分歧的地方记录在本文档 |
| 深拷贝带来的性能开销 | 图规模在几十个节点量级，可以忽略 |
| 重构期间与功能分支冲突 | 每阶段单独提交；阶段 2/3 按文件逐个迁移，不搞大爆炸式重写 |

## 9. 完成标准

- `_save_graph` 只能经由 `GraphTx.commit` 调用，op 中不再直接调用。
- `fold_ops.py` / `graph_ops.py` 中不再直接对 `nodes/edges/children/inner_edges` 做赋值或 append（可用 grep 检查）。
- 不变式默认 `strict`，全部测试绿，新增往返性质测试通过。
- 对 agent 而言，CLI / MCP 的行为没有可观察的变化。
