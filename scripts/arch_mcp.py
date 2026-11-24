#!/usr/bin/env python3
"""
arch_mcp.py - Zero-dependency Stdio MCP (Model Context Protocol) Server
exposing native Tool Calls backed by arch_engine state machine.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure the sibling arch_engine package is importable regardless of working directory
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import arch_engine as engine  # noqa: E402
from arch_engine.common import (  # noqa: E402
    ADVANCED_COMMANDS, BASIC_PRESETS, MODES, _project_mode, _require_command,
)

# The engine keeps state in a relative `.arch/` directory. Hosts launch MCP
# servers from different working directories (Claude Desktop and global configs
# often do not use the project folder), so ARCH_ROOT pins the project root.
_ARCH_ROOT = os.environ.get("ARCH_ROOT", "").strip()
if _ARCH_ROOT:
    os.chdir(os.path.expanduser(_ARCH_ROOT))


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "arch_bootstrap",
        "description": "将零依赖 CLI 引擎拷贝到项目的 .arch/scripts/ 下，后续可直接用 python3 .arch/scripts/arch_cli.py 运行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_dir": {"type": "string", "description": "可选：目标目录，默认 .arch/scripts"},
            },
        },
    },
    {
        "name": "arch_init",
        "description": "初始化项目架构图状态机（支持 pipeline / star / ring 拓扑与项目 preset 护栏）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seed": {"type": "string", "description": "用户最初模糊想法的一句话总结（北极星锚点）"},
                "topology": {
                    "type": "string",
                    "enum": list(engine.TOPOLOGIES),
                    "description": "可选：顶层拓扑（pipeline / star / ring，默认随 preset）",
                },
                "preset": {
                    "type": "string",
                    "enum": list(engine.PRESETS),
                    "description": "可选：项目护栏预设（standard / lean / agent / platform，默认 standard）",
                },
                "embed": {"type": "boolean", "description": "可选：同时把 CLI 脚本内嵌到 .arch/scripts/"},
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": "可选：basic（默认，pipeline + standard/lean 最小路径）或 advanced；星形/环形拓扑与 agent/platform 预设自动用 advanced",
                },
            },
            "required": ["seed"],
        },
    },
    {
        "name": "arch_focus",
        "description": "获取当前焦点 '?' 空洞节点的 1-Hop 上下文切片、北极星约束与最小闭环状态（支持 scope 下钻子系统）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "可选：指定聚焦的节点 ID；不传则自动聚焦首个未决空洞"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID（留空聚焦顶层）"},
            },
        },
    },
    {
        "name": "arch_collapse",
        "description": "确定待定节点或更新已确定节点的规范，并生成 Shadow Git 快照。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "目标节点 ID"},
                "label": {"type": "string", "description": "锁定后的简洁业务/模块名称"},
                "spec": {"type": "string", "description": "确定性的功能职责与技术原语描述"},
                "file_path": {"type": "string", "description": "可选：预期落地的代码文件路径"},
                "in_contract": {"type": "string", "description": "可选：覆盖全部入边的契约"},
                "out_contract": {"type": "string", "description": "可选：覆盖全部出边的契约"},
                "node_type": {
                    "type": "string",
                    "enum": ["port", "adapter", "reality_gate"],
                    "description": "节点类型（默认 port）",
                },
                "stage": {"type": "string", "description": "可选：指定归属的生命周期 Stage 阶段名称"},
                "role": {
                    "type": "string",
                    "enum": ["entry", "exit", "hub", "spoke"],
                    "description": "可选：节点拓扑角色（entry / exit / hub / spoke）",
                },
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id", "label", "spec"],
        },
    },
    {
        "name": "arch_connect",
        "description": "在两个已有节点之间建立连线（支持 primary 主流、feedback 闭环反馈、bidirectional 双向通信）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_node": {"type": "string", "description": "起点节点 ID"},
                "to_node": {"type": "string", "description": "终点节点 ID"},
                "contract": {"type": "string", "description": "连线契约描述"},
                "kind": {
                    "type": "string",
                    "enum": list(engine.EDGE_KINDS),
                    "description": "连线类型：primary / feedback / bidirectional",
                },
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["from_node", "to_node"],
        },
    },
    {
        "name": "arch_disconnect",
        "description": "移除两个已有节点之间的连线。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_node": {"type": "string", "description": "起点节点 ID"},
                "to_node": {"type": "string", "description": "终点节点 ID"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["from_node", "to_node"],
        },
    },
    {
        "name": "arch_set_edge_contract",
        "description": "按起点和终点更新一条已有连线的契约或类型，并生成 Shadow Git 快照。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_node": {"type": "string", "description": "起点节点 ID"},
                "to_node": {"type": "string", "description": "终点节点 ID"},
                "contract": {"type": "string", "description": "新契约"},
                "kind": {"type": "string", "enum": list(engine.EDGE_KINDS), "description": "可选：更新连线类型"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["from_node", "to_node", "contract"],
        },
    },
    {
        "name": "arch_add_node",
        "description": "在图中生长新节点（可插入两个节点之间、挂载到星形中枢/环形节点，或作为 'fog' 迷雾支线暂存）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "label": {"type": "string"},
                "spec": {"type": "string"},
                "status": {"type": "string", "enum": ["hole", "solid", "fog"]},
                "node_type": {"type": "string", "enum": ["port", "adapter", "reality_gate"]},
                "stage": {"type": "string", "description": "可选：指定归属的生命周期 Stage 阶段名称"},
                "role": {"type": "string", "enum": ["entry", "exit", "hub", "spoke"]},
                "after_node": {"type": "string", "description": "可选：上游前驱节点 ID"},
                "before_node": {"type": "string", "description": "可选：下游后继节点 ID"},
                "contract": {"type": "string", "description": "连线契约描述"},
                "kind": {"type": "string", "enum": list(engine.EDGE_KINDS), "description": "可选：新连线类型；同时给出 after_node 与 before_node 时默认沿用被拆开连线的类型"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id", "label", "spec"],
        },
    },
    {
        "name": "arch_add_fog",
        "description": "快捷工具：将用户提出但暂不实现的发散想法挂入侧边战争迷雾区（fog），保持主干轻盈。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "label": {"type": "string"},
                "spec": {"type": "string"},
                "stage": {"type": "string"},
                "after_node": {"type": "string", "description": "可选：挂载在哪个节点之后"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id", "label", "spec"],
        },
    },
    {
        "name": "arch_deactivate",
        "description": "将活跃节点下线移入战争迷雾区（fog）；简单链路自动缝合上下游，星形中枢、双向连线或缝合边超上限时在原位置留空白节点占位。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "目标节点 ID"},
                "reason": {"type": "string", "description": "暂缓/移入迷雾的原因说明"},
                "bypass_contract": {"type": "string", "description": "可选：上下游缝合连线的新契约"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "arch_activate",
        "description": "将战争迷雾区（fog）中的储备节点激活为待定空洞（hole）并重新接入主干连线。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "目标迷雾节点 ID"},
                "after_node": {"type": "string", "description": "可选：挂载在哪个上游节点之后"},
                "before_node": {"type": "string", "description": "可选：插入在哪个下游节点之前"},
                "contract": {"type": "string", "description": "连线契约描述"},
                "kind": {"type": "string", "enum": list(engine.EDGE_KINDS), "description": "可选：新连线类型；同时给出 after_node 与 before_node 时默认沿用被拆开连线的类型"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "arch_fold",
        "description": (
            "将已确定（solid / super_node）、彼此连通的子图折叠为一个超级节点（支持声明独立拓扑）。"
            "子节点与内部连线完整保存在超级节点内，对外连线改接到超级节点并保留契约。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_ids": {"type": "array", "items": {"type": "string"}, "description": "待折叠的节点 ID（至少 2 个）"},
                "super_id": {"type": "string", "description": "超级节点 ID"},
                "label": {"type": "string", "description": "模块名称"},
                "spec": {"type": "string", "description": "模块整体职责描述"},
                "stage": {"type": "string", "description": "可选：所属 Stage；缺省时若子节点同属一个 Stage 则沿用"},
                "topology": {"type": "string", "enum": list(engine.TOPOLOGIES), "description": "可选：子系统内部拓扑"},
                "scope": {"type": "string", "description": "可选：父级子系统 ID"},
            },
            "required": ["node_ids", "super_id", "label", "spec"],
        },
    },
    {
        "name": "arch_promote",
        "description": "自顶向下将单个节点升级为带独立拓扑（pipeline / star / ring）的子系统超级节点，内部生成初始骨架并保留外部边界端口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "待升级的节点 ID"},
                "topology": {"type": "string", "enum": list(engine.TOPOLOGIES), "description": "子系统拓扑：pipeline / star / ring"},
                "label": {"type": "string", "description": "可选：更新子系统名称"},
                "spec": {"type": "string", "description": "可选：更新子系统职责"},
                "scope": {"type": "string", "description": "可选：父级子系统 ID"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "arch_unfold",
        "description": (
            "展开超级节点，原样恢复子节点、内部连线与对外连线。折叠期间新增的连线自动接到唯一的入口/出口子节点；"
            "无法自动归位时会报错并列出候选，此时向用户确认后通过 attach 指定。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "超级节点 ID"},
                "attach": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "可选：{外部节点ID: 子节点ID}，指定折叠期间新增连线接到哪个子节点",
                },
                "scope": {"type": "string", "description": "可选：父级子系统 ID"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "arch_verify",
        "description": (
            "沉淀已验证的客观事实与架构硬约束（结构化记录在 .arch/records.json，ground_truth.md 由工具生成）。"
            "只与部分节点相关的事实务必给 scope，global 事实每次 focus 都会带上；过时的事实用 supersedes 取代，不要重复追加。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "已验证的客观现实结论"},
                "constraint": {"type": "string", "description": "由此推导出的架构硬约束"},
                "scope": {
                    "type": "array", "items": {"type": "string"},
                    "description": "可选：作用域，global（默认）、节点 ID 或阶段名",
                },
                "source": {"type": "string", "description": "可选：证据来源路径，如 analysis/x/case.json#key"},
                "supersedes": {
                    "type": "array", "items": {"type": "string"},
                    "description": "可选：被本条取代的旧事实编号（GT-xx）",
                },
            },
            "required": ["fact", "constraint"],
        },
    },
    {
        "name": "arch_set_node_state",
        "description": (
            "记录节点的实现进度（impl）、验证程度（verify，自动记日期）、引用（refs，如 docs/protocol.md#6，"
            "export_task 会内联该章节）与实现文件（可多个）。可直接用于折叠模块内的子节点。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "impl": {"type": "string", "enum": ["planned", "building", "built"]},
                "verify": {"type": "string", "enum": ["none", "unit", "api", "device"]},
                "verify_date": {"type": "string", "description": "可选：YYYY-MM-DD，缺省为今天"},
                "refs": {"type": "array", "items": {"type": "string"}, "description": "可选：替换整个引用列表"},
                "add_refs": {"type": "array", "items": {"type": "string"}},
                "drop_refs": {"type": "array", "items": {"type": "string"}},
                "file_paths": {"type": "array", "items": {"type": "string"}, "description": "可选：替换实现文件列表"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "arch_open",
        "description": (
            "待办项登记：action=add 登记待验证的未知项（真机实测 / 调研 / 待决策，由用户或代理处理，是否卡开发）；"
            "action=resolve 写结论关闭，带 constraint 时同时沉淀为同作用域的事实；action=list 列出。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "resolve", "list"]},
                "question": {"type": "string", "description": "add：要弄清的问题"},
                "kind": {"type": "string", "enum": ["device_test", "research", "decision"]},
                "owner": {"type": "string", "enum": ["user", "agent"]},
                "scope": {"type": "array", "items": {"type": "string"},
                          "description": "add：作用域，global（默认）、节点 ID 或阶段名；list：按作用域精确筛选（取第一个）"},
                "blocking": {"type": "boolean", "description": "add：是否卡住开发；list：只列卡开发的"},
                "id": {"type": "string", "description": "resolve：待办项编号 OI-xx"},
                "resolution": {"type": "string", "description": "resolve：结论"},
                "constraint": {"type": "string", "description": "resolve：可选，同时沉淀为事实的架构约束"},
                "node_id": {"type": "string", "description": "list：与该节点相关的"},
                "include_resolved": {"type": "boolean", "description": "list：包括已解决的"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "arch_facts",
        "description": "查询事实：缺省列 global 事实及字数；node_id 列与该节点（含阶段、折叠子节点）相关的事实和墓志；all=true 按作用域列全部。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "all": {"type": "boolean"},
                "with_superseded": {"type": "boolean", "description": "包括已被取代的事实"},
            },
        },
    },
    {
        "name": "arch_status",
        "description": "项目进度总览：拓扑与预设、各节点实现进度与验证程度、未关闭与卡开发的待办项、记录统计与迁移提示。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "arch_migrate_records",
        "description": (
            "把旧格式 ground_truth.md / graveyard.md 原文按条目 / 标题切块，一字不改地存为待处理记录（幂等）。"
            "引擎不做分类：之后逐条 arch_legacy show，按类型用 arch_verify / arch_graveyard_add / arch_open / "
            "arch_set_node_state 记录，再 arch_legacy resolve；列表为空才算迁移完成。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "arch_graveyard_add",
        "description": "记入一条墓志（被废弃的方案与原因），不重置节点。迁移旧墓志时使用；实现踩坑需要换方案时用 arch_reset_node。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "scheme": {"type": "string", "description": "被废弃的方案"},
                "reason": {"type": "string", "description": "废弃原因"},
            },
            "required": ["node_id", "scheme", "reason"],
        },
    },
    {
        "name": "arch_rescope",
        "description": "修改事实 (GT-xx) 或待办项 (OI-xx) 的作用域，内容不变。迁移后把只与部分节点相关的 global 事实收窄时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id", "scope"],
        },
    },
    {
        "name": "arch_legacy",
        "description": (
            "迁移原文（LG-xx）：action=list 列出并给出处理步骤，show 看全文（不传 id 显示下一条待处理），"
            "resolve 标记已处理并在 note 写明拆成了哪些记录（原文仍保留在 records.json）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "show", "resolve"]},
                "id": {"type": "string", "description": "show（可选）/ resolve：LG-xx"},
                "note": {"type": "string", "description": "resolve：处理去向，如“拆为 GT-12、OI-03”"},
                "include_resolved": {"type": "boolean", "description": "list：包括已处理的"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "arch_config",
        "description": "查看或设置项目模式（basic / advanced）、preset 护栏与记录配置（max_chain_len、max_active_holes、allow_feedback、global_fact_limit 等）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(MODES),
                    "description": "basic：只走流水线最小路径；advanced：解锁星形/环形拓扑、子系统（fold/promote/unfold）与记录维护工具。切换前先征得用户同意",
                },
                "preset": {"type": "string", "enum": list(engine.PRESETS)},
                "default_topology": {"type": "string", "enum": list(engine.TOPOLOGIES)},
                "max_depth": {"type": "integer"},
                "max_chain_len": {"type": "integer"},
                "max_hub_degree": {"type": "integer"},
                "max_cycle_len": {"type": "integer"},
                "max_active_holes": {"type": "integer"},
                "max_unfolded_solid": {"type": "integer"},
                "auto_bridge_limit": {"type": "integer"},
                "allow_feedback": {"type": "boolean"},
                "global_fact_limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "arch_ground",
        "description": "向可插拔 Grounding 设置器（.arch/grounding.json）动态注册文档、命令行探针或人工验证源。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["human_verify", "doc_ref", "probe_cmd", "code_ref"],
                },
                "source": {"type": "string", "description": "文件路径、命令行探针或验证说明"},
                "note": {"type": "string", "description": "可选补充说明"},
            },
            "required": ["source_type", "source"],
        },
    },
    {
        "name": "arch_export_task",
        "description": "导出单个已确定节点的独立编码任务卡（含 Shadow Git 人工防覆盖校验），专供独立子代理在沙箱中执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "要导出任务的节点 ID"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "arch_commit_node",
        "description": "子代理完成单节点代码编写并测试通过后，提交 Shadow Git 快照并绑定代码文件路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "file_path": {"type": "string", "description": "实现文件（项目相对路径）"},
                "file_paths": {
                    "type": "array", "items": {"type": "string"},
                    "description": "可选：节点由多个文件实现时传全部路径，代替 file_path",
                },
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "arch_sync_manual",
        "description": "当检测到人类在 IDE 手动修改了节点代码后，同步修改摘要并更新影子仓库快照以解除覆盖拦截。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "summary": {"type": "string", "description": "一句话总结人类手动修改的逻辑"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id", "summary"],
        },
    },
    {
        "name": "arch_reset_node",
        "description": "现实运行踩坑时，保持主干拓扑不变，将报错叶子节点重置为 '?' 待定状态并把失败原因记录进 graveyard.md 避坑备忘。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "reason": {"type": "string", "description": "踩坑原因（自动记入 graveyard.md）"},
                "scope": {"type": "string", "description": "可选：子系统超级节点 ID"},
            },
            "required": ["node_id", "reason"],
        },
    },
    {
        "name": "arch_fork",
        "description": "分支回滚：从指定 Shadow Git 快照标签安全回滚图状态（自动保留所有历史避坑记录、现实事实与项目预设）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_tag": {"type": "string", "description": "目标历史快照标签，如 arch-step-02"},
            },
            "required": ["from_tag"],
        },
    },
    {
        "name": "arch_export_ui",
        "description": "导出单文件多拓扑交互式架构看板 HTML（支持泳道 / 星形 / 环形布局切换与子系统下钻），离线可用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "out_path": {"type": "string", "description": "可选：输出 HTML 路径，默认 .arch/dashboard.html"},
            },
        },
    },
]


def _command_of(tool_name: str) -> str:
    """CLI command behind an MCP tool (`arch_graveyard_add` -> `graveyard`)."""
    cmd = tool_name[len("arch_"):].replace("_", "-") if tool_name.startswith("arch_") else tool_name
    return "graveyard" if cmd == "graveyard-add" else cmd


# Parameters only advanced projects use; basic mode leaves them out of the
# tool list to keep it small. Calls that still pass them work as before.
_ADVANCED_PARAMS = {"scope"}
_ADVANCED_CONFIG_PARAMS = {"default_topology", "max_depth", "max_hub_degree", "max_cycle_len"}


def _tools_for_mode(mode: str) -> List[Dict[str, Any]]:
    """The tool list a project in `mode` sees."""
    if mode != "basic":
        return TOOLS
    out: List[Dict[str, Any]] = []
    for tool in TOOLS:
        if _command_of(tool["name"]) in ADVANCED_COMMANDS:
            continue
        schema = dict(tool["inputSchema"])
        drop = _ADVANCED_PARAMS | (_ADVANCED_CONFIG_PARAMS if tool["name"] == "arch_config" else set())
        props = {k: v for k, v in (schema.get("properties") or {}).items() if k not in drop}
        if "role" in props:
            props["role"] = {**props["role"], "enum": ["entry", "exit"]}
        if tool["name"] == "arch_config" and "preset" in props:
            props["preset"] = {**props["preset"], "enum": list(BASIC_PRESETS)}
        schema["properties"] = props
        out.append({**tool, "inputSchema": schema})
    return out


def _dispatch_tool(name: str, args: Dict[str, Any]) -> str:
    _require_command(_command_of(name))
    if name == "arch_bootstrap":
        return engine.op_bootstrap(args.get("target_dir", ""))
    if name == "arch_init":
        return engine.op_init(
            args["seed"],
            topology=args.get("topology", ""),
            preset=args.get("preset", "standard"),
            embed=bool(args.get("embed", False)),
            mode=args.get("mode", ""),
        )
    if name == "arch_focus":
        return engine.op_focus(args.get("node_id"), scope=args.get("scope", ""))
    if name == "arch_collapse":
        return engine.op_collapse(
            node_id=args["node_id"],
            label=args["label"],
            spec=args["spec"],
            file_path=args.get("file_path", ""),
            in_contract=args.get("in_contract", ""),
            out_contract=args.get("out_contract", ""),
            node_type=args.get("node_type", "port"),
            stage=args.get("stage", ""),
            role=args.get("role", ""),
            scope=args.get("scope", ""),
        )
    if name == "arch_connect":
        return engine.op_connect(
            from_node=args["from_node"],
            to_node=args["to_node"],
            contract=args.get("contract", "TBD"),
            kind=args.get("kind", "primary"),
            scope=args.get("scope", ""),
        )
    if name == "arch_disconnect":
        return engine.op_disconnect(
            from_node=args["from_node"],
            to_node=args["to_node"],
            scope=args.get("scope", ""),
        )
    if name == "arch_set_edge_contract":
        return engine.op_set_edge_contract(
            args["from_node"],
            args["to_node"],
            args["contract"],
            kind=args.get("kind", ""),
            scope=args.get("scope", ""),
        )
    if name == "arch_add_node":
        return engine.op_add_node(
            node_id=args["node_id"],
            label=args["label"],
            spec=args["spec"],
            status=args.get("status", "hole"),
            node_type=args.get("node_type", "port"),
            stage=args.get("stage", ""),
            after_node=args.get("after_node", ""),
            before_node=args.get("before_node", ""),
            contract=args.get("contract", "TBD"),
            role=args.get("role", ""),
            edge_kind=args.get("kind", ""),
            scope=args.get("scope", ""),
        )
    if name == "arch_add_fog":
        return engine.op_add_node(
            node_id=args["node_id"],
            label=args["label"],
            spec=args["spec"],
            status="fog",
            node_type="port",
            stage=args.get("stage", ""),
            after_node=args.get("after_node", ""),
            contract="optional_fog",
            scope=args.get("scope", ""),
        )
    if name == "arch_deactivate":
        return engine.op_deactivate_node(
            node_id=args["node_id"],
            reason=args.get("reason", ""),
            bypass_contract=args.get("bypass_contract", ""),
            scope=args.get("scope", ""),
        )
    if name == "arch_activate":
        return engine.op_activate_node(
            node_id=args["node_id"],
            after_node=args.get("after_node", ""),
            before_node=args.get("before_node", ""),
            contract=args.get("contract", "TBD"),
            edge_kind=args.get("kind", ""),
            scope=args.get("scope", ""),
        )
    if name == "arch_fold":
        return engine.op_fold(
            node_ids=args["node_ids"],
            super_id=args["super_id"],
            label=args["label"],
            spec=args["spec"],
            stage=args.get("stage", ""),
            topology=args.get("topology", ""),
            scope=args.get("scope", ""),
        )
    if name == "arch_promote":
        return engine.op_promote(
            node_id=args["node_id"],
            topology=args.get("topology", ""),
            label=args.get("label", ""),
            spec=args.get("spec", ""),
            scope=args.get("scope", ""),
        )
    if name == "arch_unfold":
        return engine.op_unfold(args["node_id"], args.get("attach") or {}, scope=args.get("scope", ""))
    if name == "arch_verify":
        return engine.op_verify(
            args["fact"], args["constraint"],
            scope=args.get("scope"), source=args.get("source", ""), supersedes=args.get("supersedes"),
        )
    if name == "arch_set_node_state":
        return engine.op_set_node_state(
            args["node_id"],
            impl=args.get("impl", ""),
            verify=args.get("verify", ""),
            verify_date=args.get("verify_date", ""),
            refs=args.get("refs"),
            add_refs=args.get("add_refs"),
            drop_refs=args.get("drop_refs"),
            file_paths=args.get("file_paths"),
        )
    if name == "arch_open":
        action = args.get("action", "list")
        if action == "add":
            return engine.op_open_add(
                args.get("question", ""), args.get("kind", ""), args.get("owner", ""),
                scope=args.get("scope"), blocking=bool(args.get("blocking")),
            )
        if action == "resolve":
            return engine.op_open_resolve(args.get("id", ""), args.get("resolution", ""), args.get("constraint", ""))
        if action == "list":
            scope = args.get("scope") or ""
            if isinstance(scope, list):
                scope = scope[0] if scope else ""
            return engine.op_open_list(
                node_id=args.get("node_id", ""), scope=scope,
                include_resolved=bool(args.get("include_resolved")),
                owner=args.get("owner", ""), blocking_only=bool(args.get("blocking")),
            )
        raise engine.ArchEngineError(f"arch_open 的 action 只能是 add / resolve / list，收到：{action}")
    if name == "arch_facts":
        return engine.op_facts(args.get("node_id", ""), bool(args.get("all")), bool(args.get("with_superseded")))
    if name == "arch_status":
        return engine.op_status()
    if name == "arch_migrate_records":
        return engine.op_migrate_records()
    if name == "arch_rescope":
        return engine.op_rescope(args["id"], args["scope"])
    if name == "arch_graveyard_add":
        return engine.op_graveyard_add(args["node_id"], args["scheme"], args["reason"])
    if name == "arch_legacy":
        return engine.op_legacy(
            args.get("action", "list"), args.get("id", ""), args.get("note", ""), bool(args.get("include_resolved")),
        )
    if name == "arch_config":
        return engine.op_config(
            global_fact_limit=args.get("global_fact_limit"),
            preset=args.get("preset"),
            default_topology=args.get("default_topology"),
            max_depth=args.get("max_depth"),
            max_chain_len=args.get("max_chain_len"),
            max_hub_degree=args.get("max_hub_degree"),
            max_cycle_len=args.get("max_cycle_len"),
            max_active_holes=args.get("max_active_holes"),
            max_unfolded_solid=args.get("max_unfolded_solid"),
            auto_bridge_limit=args.get("auto_bridge_limit"),
            allow_feedback=args.get("allow_feedback"),
            mode=args.get("mode"),
        )
    if name == "arch_ground":
        return engine.op_ground(args["source_type"], args["source"], args.get("note", ""))
    if name == "arch_export_task":
        return engine.op_export_task(args["node_id"], scope=args.get("scope", ""))
    if name == "arch_commit_node":
        return engine.op_commit_node(
            args["node_id"], args.get("file_paths") or args.get("file_path", ""), scope=args.get("scope", ""),
        )
    if name == "arch_sync_manual":
        return engine.op_sync_manual(args["node_id"], args["summary"], scope=args.get("scope", ""))
    if name == "arch_reset_node":
        return engine.op_reset_node(args["node_id"], args["reason"], scope=args.get("scope", ""))
    if name == "arch_fork":
        return engine.op_fork(args["from_tag"])
    if name == "arch_export_ui":
        return engine.op_export_ui(args.get("out_path", ""))
    raise engine.ArchEngineError(f"未知 MCP 工具：{name}")


def _send_response(msg_id: Any, result: Dict[str, Any]) -> None:
    payload = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send_notification(method: str) -> None:
    payload = {"jsonrpc": "2.0", "method": method}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send_error(msg_id: Any, code: int, message: str) -> None:
    payload = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # Notifications without id do not expect a response
        if msg_id is None:
            continue

        if method == "initialize":
            _send_response(
                msg_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "arch-grower-mcp", "version": engine.ENGINE_VERSION},
                },
            )
        elif method == "tools/list":
            _send_response(msg_id, {"tools": _tools_for_mode(_project_mode())})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            mode_before = _project_mode()
            try:
                output_text = _dispatch_tool(tool_name, tool_args)
                _send_response(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": output_text}],
                        "isError": False,
                    },
                )
                if _project_mode() != mode_before:
                    _send_notification("notifications/tools/list_changed")
            except engine.ArchEngineError as exc:
                _send_response(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                )
            except Exception as exc:
                _send_response(
                    msg_id,
                    {
                        "content": [{"type": "text", "text": f"Internal Error: {exc}"}],
                        "isError": True,
                    },
                )
        else:
            _send_error(msg_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    serve_stdio()
