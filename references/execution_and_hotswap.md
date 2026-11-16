# 子代理隔离执行、防漂移与局部热替换协议

## 1\. 曳光弹冻结与子代理隔离编码

当 `arch_focus` 提示 **`[WALKING_SKELETON_READY]`**（主干通路已无 `hole`）且用户确认开写后：

1. 按拓扑顺序，逐个调用 `arch_export_task`（或运行 `python3 scripts/arch_cli.py export-task --node <node_id>`）导出单节点孤立任务书（自动附带 1-hop 契约与 `ground_truth.md` / `graveyard.md` 约束）。  
2. \*\*拉起独立子代理（Sub-Agent）\*\*执行任务书，并在子代理提示词中强制要求：  
   - 采用**端口与适配器分离（Port/Adapter）**，将易变的外部接口隔离在独立适配器文件中；  
   - 为上下游连线编写契约校验与单元测试；若含 `reality_gate` 节点，生成终端交互式测试桩；  
   - 测试通过后，调用 `arch_commit_node`（或运行 `python3 scripts/arch_cli.py commit-node --node <node_id>`）提交 Shadow Git 快照；  
   - **仅向主控窗口返回不超过 3 行的完成摘要**，严禁把代码细节带回主对话。

## 2\. Shadow Git 防漂移处理

* **`ManualDriftError`（人类在 IDE 手动改了内部实现）**：严禁直接覆盖。拉起轻量子代理总结人类改动，调用 `arch_sync_manual` 更新快照后再继续。  
* **`ContractDriftError`（人类改动破坏了上下游函数签名）**：暂停生成，向用户确认是以当前代码为准同步更新下游节点，还是回退签名。

## 3\. 现实踩坑与局部热替换（Hot-Swap）

* **单节点定点替换**：运行中发现某个外部接口有 Bug 时，保持主干连线不动，调用 `arch_reset_node`（传入 `node` 与 `reason`，自动写入 `graveyard.md`），将该节点退化为 `🟡 ?` 空洞，重新发起一轮二选一提问替换适配器。  
* **分支时间旅行**：若整条探索路线走入死胡同，调用 `arch_fork` 回滚图状态（保留 `graveyard.md` 避坑记录），开启新分支探索。