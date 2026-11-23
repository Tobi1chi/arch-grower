#!/usr/bin/env python3
"""
arch_cli.py - Zero-dependency State Machine, 1-Hop Slicer & Shadow Git Guardrail
for Autoregressive Architecture Co-Creation.

Command-line entry point. The engine lives in the sibling package
`arch_engine/` (see its __init__ for the module map); this file only parses
arguments and prints what the operations return.
"""

import argparse
import sys

from arch_engine import (
    ArchEngineError, EDGE_KINDS, ENGINE_VERSION, NODE_ROLES, PRESETS, TOPOLOGIES,
    op_activate_node, op_add_node, op_bootstrap, op_collapse, op_commit_node, op_config,
    op_connect, op_deactivate_node, op_disconnect, op_export_task, op_export_ui, op_facts,
    op_focus, op_fold, op_fork, op_graveyard_add, op_ground, op_init, op_legacy,
    op_migrate_records, op_open_add, op_open_list, op_open_resolve, op_promote, op_rescope,
    op_reset_node, op_set_edge_contract, op_set_node_state, op_status, op_sync_manual,
    op_unfold, op_verify,
)
from arch_engine.common import (
    REF_WHOLE_FILE_LIMIT, _load_config, _load_graph, _save_config, _save_graph, _today,
)
from arch_engine.graph import (
    IMPL_LEVELS, SIGNATURE_LINE, VERIFY_LEVELS, _find_any_node, _find_walking_skeleton,
    _node_state,
)
from arch_engine.records import (
    DEFAULT_TITLES, GENERATED_MARKER, MD_HEADING, OPEN_KINDS, OPEN_OWNERS,
    _global_fact_budget, _read_records, _save_records,
)
from arch_engine.refs import _extract_md_section

# Everything importable from arch_cli before the engine moved into arch_engine/
# stays importable from here (tests and ad-hoc scripts rely on it).
__all__ = [
    "main", "ArchEngineError", "ENGINE_VERSION", "PRESETS", "TOPOLOGIES", "EDGE_KINDS", "NODE_ROLES",
    "op_activate_node", "op_add_node", "op_bootstrap", "op_collapse", "op_commit_node", "op_config",
    "op_connect", "op_deactivate_node", "op_disconnect", "op_export_task", "op_export_ui",
    "op_facts", "op_focus", "op_fold", "op_fork", "op_graveyard_add", "op_ground", "op_init",
    "op_legacy", "op_migrate_records", "op_open_add", "op_open_list", "op_open_resolve",
    "op_promote", "op_rescope", "op_reset_node", "op_set_edge_contract", "op_set_node_state",
    "op_status", "op_sync_manual", "op_unfold", "op_verify",
    "REF_WHOLE_FILE_LIMIT", "_load_config", "_load_graph", "_save_config", "_save_graph", "_today",
    "SIGNATURE_LINE", "_find_any_node", "_find_walking_skeleton", "_node_state",
    "DEFAULT_TITLES", "GENERATED_MARKER", "MD_HEADING",
    "_global_fact_budget", "_read_records", "_save_records", "_extract_md_section",
]


def _parse_bool(raw: str) -> bool:
    v = raw.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"布尔值应为 true / false，收到：{raw}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Autoregressive Architecture State Machine CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_boot = sub.add_parser("bootstrap", help="将零依赖 CLI 引擎拷贝到当前项目的 .arch/scripts/ 下直接运行")
    p_boot.add_argument("--out", default="", help="可选：自定义输出目录，默认 .arch/scripts")

    p_init = sub.add_parser("init", help="初始化项目架构图、预设配置与 Shadow Git")
    p_init.add_argument("--seed", required=True)
    p_init.add_argument("--topology", default="", choices=["", *TOPOLOGIES], help="顶层拓扑：pipeline / star / ring")
    p_init.add_argument("--preset", default="standard", choices=list(PRESETS), help="项目预设护栏：standard / lean / agent / platform")
    p_init.add_argument("--embed", action="store_true", help="初始化时顺带把 CLI 脚本拷贝进 .arch/scripts/")

    p_focus = sub.add_parser("focus")
    p_focus.add_argument("--node", default=None)
    p_focus.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID（留空为顶层）")

    p_col = sub.add_parser("collapse")
    p_col.add_argument("--node", required=True)
    p_col.add_argument("--label", required=True)
    p_col.add_argument("--spec", required=True)
    p_col.add_argument("--file-path", default="")
    p_col.add_argument("--in-contract", default="")
    p_col.add_argument("--out-contract", default="")
    p_col.add_argument("--type", default="port")
    p_col.add_argument("--stage", default="")
    p_col.add_argument("--role", default="", choices=list(NODE_ROLES), help="拓扑角色：entry / exit / hub / spoke")
    p_col.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_conn = sub.add_parser("connect", help="在两个已有节点间建立连线（支持回流边与双向边）")
    p_conn.add_argument("--from", dest="from_node", required=True)
    p_conn.add_argument("--to", dest="to_node", required=True)
    p_conn.add_argument("--contract", default="TBD")
    p_conn.add_argument("--kind", default="primary", choices=list(EDGE_KINDS))
    p_conn.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_disc = sub.add_parser("disconnect", help="移除两个已有节点间的连线")
    p_disc.add_argument("--from", dest="from_node", required=True)
    p_disc.add_argument("--to", dest="to_node", required=True)
    p_disc.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_edge = sub.add_parser("set-edge-contract", help="更新指定连线的契约或类型")
    p_edge.add_argument("--from", dest="from_node", required=True)
    p_edge.add_argument("--to", dest="to_node", required=True)
    p_edge.add_argument("--contract", required=True)
    p_edge.add_argument("--kind", default="", choices=["", *EDGE_KINDS])
    p_edge.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_add = sub.add_parser("add-node")
    p_add.add_argument("--node", required=True)
    p_add.add_argument("--label", required=True)
    p_add.add_argument("--spec", required=True)
    p_add.add_argument("--status", default="hole", choices=["hole", "solid", "fog"])
    p_add.add_argument("--type", default="port")
    p_add.add_argument("--stage", default="")
    p_add.add_argument("--role", default="", choices=list(NODE_ROLES))
    p_add.add_argument("--after", default="")
    p_add.add_argument("--before", default="")
    p_add.add_argument("--contract", default="TBD")
    p_add.add_argument("--kind", default="primary", choices=list(EDGE_KINDS))
    p_add.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_exp_ui = sub.add_parser("export-ui", help="导出多拓扑交互式架构看板 HTML（支持子系统下钻）")
    p_exp_ui.add_argument("--out", default="")

    p_deact = sub.add_parser("deactivate", help="将活跃节点下线并移入战争迷雾区，自动缝合上下游边")
    p_deact.add_argument("--node", required=True)
    p_deact.add_argument("--reason", default="")
    p_deact.add_argument("--bypass-contract", default="")
    p_deact.add_argument("--no-bridge", action="store_true", help="直接断开关联连线，不自动缝合旁路")
    p_deact.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_act = sub.add_parser("activate", help="将迷雾区节点激活为待定空洞并接入主干")
    p_act.add_argument("--node", required=True)
    p_act.add_argument("--after", default="")
    p_act.add_argument("--before", default="")
    p_act.add_argument("--contract", default="TBD")
    p_act.add_argument("--kind", default="primary", choices=list(EDGE_KINDS))
    p_act.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_fold = sub.add_parser("fold", help="将已确定、相互连通的子图折叠为一个超级节点")
    p_fold.add_argument("--nodes", required=True, help="逗号分隔的待折叠节点 ID，如 a,b,c")
    p_fold.add_argument("--id", required=True, help="超级节点 ID")
    p_fold.add_argument("--label", required=True)
    p_fold.add_argument("--spec", required=True)
    p_fold.add_argument("--stage", default="")
    p_fold.add_argument("--topology", default="", choices=["", *TOPOLOGIES], help="子系统内部拓扑")
    p_fold.add_argument("--in", dest="scope", default="", help="父级子系统 ID")

    p_prom = sub.add_parser("promote", help="自顶向下将单个节点升级为带独立拓扑的子系统超级节点")
    p_prom.add_argument("--node", required=True)
    p_prom.add_argument("--topology", default="", choices=["", *TOPOLOGIES], help="子系统内部拓扑：pipeline / star / ring")
    p_prom.add_argument("--label", default="")
    p_prom.add_argument("--spec", default="")
    p_prom.add_argument("--in", dest="scope", default="", help="父级子系统 ID")

    p_unfold = sub.add_parser("unfold", help="展开超级节点，恢复其子节点与连线")
    p_unfold.add_argument("--node", required=True)
    p_unfold.add_argument(
        "--attach", action="append", default=[], metavar="NEIGHBOUR=CHILD",
        help="折叠期间新增连线的归位：外部节点ID=子节点ID（可重复）",
    )
    p_unfold.add_argument("--in", dest="scope", default="", help="父级子系统 ID")

    p_ver = sub.add_parser("verify", help="沉淀已验证事实与架构约束")
    p_ver.add_argument("--fact", required=True)
    p_ver.add_argument("--constraint", required=True)
    p_ver.add_argument("--scope", action="append", default=[],
                       help="作用域：global（默认）、节点 ID 或阶段名，可重复；需已迁移为结构化记录")
    p_ver.add_argument("--source", default="", help="证据来源，如 analysis/x/case.json#key 或实测记录路径")
    p_ver.add_argument("--supersedes", action="append", default=[], metavar="GT-xx",
                       help="本条取代的旧事实编号，可重复；被取代的条目默认不再展示")

    p_state = sub.add_parser("set-node-state", help="记录节点的实现进度、验证程度、引用与实现文件")
    p_state.add_argument("--node", required=True)
    p_state.add_argument("--impl", default="", choices=["", *IMPL_LEVELS])
    p_state.add_argument("--verify", default="", choices=["", *VERIFY_LEVELS])
    p_state.add_argument("--verify-date", default="", help="YYYY-MM-DD，缺省为今天")
    p_state.add_argument("--ref", action="append", default=None,
                         help="替换整个引用列表，可重复，如 docs/protocol.md#6")
    p_state.add_argument("--add-ref", action="append", default=[])
    p_state.add_argument("--drop-ref", action="append", default=[])
    p_state.add_argument("--file-path", action="append", default=None, help="替换实现文件列表，可重复")

    p_open = sub.add_parser("open", help="待办项登记：add / resolve / list")
    open_sub = p_open.add_subparsers(dest="open_cmd", required=True)
    p_oadd = open_sub.add_parser("add")
    p_oadd.add_argument("--question", required=True)
    p_oadd.add_argument("--kind", required=True, choices=list(OPEN_KINDS))
    p_oadd.add_argument("--owner", required=True, choices=list(OPEN_OWNERS))
    p_oadd.add_argument("--scope", action="append", default=[], help="global（默认）、节点 ID 或阶段名，可重复")
    p_oadd.add_argument("--blocking", action="store_true", help="卡住开发")
    p_ores = open_sub.add_parser("resolve")
    p_ores.add_argument("--id", required=True)
    p_ores.add_argument("--resolution", required=True)
    p_ores.add_argument("--constraint", default="", help="给出时把结论同时沉淀为同作用域的事实")
    p_olist = open_sub.add_parser("list")
    p_olist.add_argument("--node", default="", help="与该节点相关（含阶段、折叠子节点）")
    p_olist.add_argument("--scope", default="", help="作用域精确匹配，如 global")
    p_olist.add_argument("--owner", default="", choices=["", *OPEN_OWNERS])
    p_olist.add_argument("--blocking", action="store_true")
    p_olist.add_argument("--all", action="store_true", help="包括已解决的")

    p_facts = sub.add_parser("facts", help="查询事实：缺省列 global，--node 列节点相关，--all 列全部")
    p_facts.add_argument("--node", default="")
    p_facts.add_argument("--all", action="store_true")
    p_facts.add_argument("--with-superseded", action="store_true")

    sub.add_parser("status", help="项目进度总览：拓扑/预设、节点实现/验证、待办项、记录统计")
    sub.add_parser("migrate-records", help="把旧格式 ground_truth.md / graveyard.md 原文切块存入 .arch/records.json 待处理（幂等）")

    p_grave = sub.add_parser("graveyard", help="墓志：add 记录废弃方案（不重置节点）")
    grave_sub = p_grave.add_subparsers(dest="grave_cmd", required=True)
    p_gadd = grave_sub.add_parser("add")
    p_gadd.add_argument("--node", required=True)
    p_gadd.add_argument("--scheme", required=True, help="被废弃的方案")
    p_gadd.add_argument("--reason", required=True)

    p_rescope = sub.add_parser("rescope", help="修改事实 (GT-xx) 或待办项 (OI-xx) 的作用域")
    p_rescope.add_argument("--id", required=True)
    p_rescope.add_argument("--scope", action="append", required=True)

    p_legacy = sub.add_parser("legacy", help="未分类遗留记录：list / show / resolve")
    legacy_sub = p_legacy.add_subparsers(dest="legacy_cmd", required=True)
    p_llist = legacy_sub.add_parser("list")
    p_llist.add_argument("--all", action="store_true", help="包括已处理的")
    p_lshow = legacy_sub.add_parser("show", help="缺省显示下一条待处理记录")
    p_lshow.add_argument("--id", default="")
    p_lres = legacy_sub.add_parser("resolve")
    p_lres.add_argument("--id", required=True)
    p_lres.add_argument("--note", default="", help="处理去向，如“拆为 GT-12、OI-03”")

    p_cfg = sub.add_parser("config", help="查看或设置项目 preset 护栏与记录配置")
    p_cfg.add_argument("--preset", default=None, choices=list(PRESETS), help="切换命名预设")
    p_cfg.add_argument("--default-topology", default=None, choices=list(TOPOLOGIES))
    p_cfg.add_argument("--max-depth", type=int, default=None, help="子系统最大嵌套深度（默认 2）")
    p_cfg.add_argument("--max-chain-len", type=int, default=None, help="流水线最大主干链长软上限")
    p_cfg.add_argument("--max-hub-degree", type=int, default=None, help="单节点最大活跃邻居数软上限")
    p_cfg.add_argument("--max-cycle-len", type=int, default=None, help="主闭环最大节点数软上限")
    p_cfg.add_argument("--max-active-holes", type=int, default=None, help="同作用域最大待定空洞硬上限")
    p_cfg.add_argument("--max-unfolded-solid", type=int, default=None, help="同作用域未折叠实心节点软上限")
    p_cfg.add_argument("--auto-bridge-limit", type=int, default=None, help="deactivate 自动缝合最大边数")
    p_cfg.add_argument("--allow-feedback", type=_parse_bool, default=None, help="是否允许 feedback 回流边（true/false）")
    p_cfg.add_argument("--global-fact-limit", type=int, default=None, help="global 事实字数上限（默认 3000）")

    p_gnd = sub.add_parser("ground")
    p_gnd.add_argument("--type", required=True, choices=["human_verify", "doc_ref", "probe_cmd", "code_ref"])
    p_gnd.add_argument("--source", required=True)
    p_gnd.add_argument("--note", default="")

    p_exp = sub.add_parser("export-task")
    p_exp.add_argument("--node", required=True)
    p_exp.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_com = sub.add_parser("commit-node")
    p_com.add_argument("--node", required=True)
    p_com.add_argument("--file-path", required=True, action="append", help="实现文件，可重复绑定多个")
    p_com.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_syn = sub.add_parser("sync-manual")
    p_syn.add_argument("--node", required=True)
    p_syn.add_argument("--summary", required=True)
    p_syn.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_rst = sub.add_parser("reset-node")
    p_rst.add_argument("--node", required=True)
    p_rst.add_argument("--reason", required=True)
    p_rst.add_argument("--in", dest="scope", default="", help="子系统超级节点 ID")

    p_frk = sub.add_parser("fork")
    p_frk.add_argument("--from-tag", required=True)

    args = parser.parse_args()
    try:
        if args.cmd == "bootstrap":
            print(op_bootstrap(args.out))
        elif args.cmd == "init":
            print(op_init(args.seed, topology=args.topology, preset=args.preset, embed=args.embed))
        elif args.cmd == "focus":
            print(op_focus(args.node, scope=args.scope))
        elif args.cmd == "collapse":
            print(op_collapse(
                args.node, args.label, args.spec,
                args.file_path, args.in_contract, args.out_contract, args.type, args.stage,
                role=args.role, scope=args.scope,
            ))
        elif args.cmd == "connect":
            print(op_connect(args.from_node, args.to_node, args.contract, kind=args.kind, scope=args.scope))
        elif args.cmd == "disconnect":
            print(op_disconnect(args.from_node, args.to_node, scope=args.scope))
        elif args.cmd == "set-edge-contract":
            print(op_set_edge_contract(
                args.from_node, args.to_node, args.contract, kind=args.kind, scope=args.scope,
            ))
        elif args.cmd == "add-node":
            print(op_add_node(
                args.node, args.label, args.spec,
                args.status, args.type, args.stage, args.after, args.before, args.contract,
                role=args.role, edge_kind=args.kind, scope=args.scope,
            ))
        elif args.cmd == "export-ui":
            print(op_export_ui(args.out))
        elif args.cmd == "deactivate":
            print(op_deactivate_node(
                args.node, args.reason, args.bypass_contract, no_bridge=args.no_bridge, scope=args.scope,
            ))
        elif args.cmd == "activate":
            print(op_activate_node(
                args.node, args.after, args.before, args.contract, edge_kind=args.kind, scope=args.scope,
            ))
        elif args.cmd == "fold":
            print(op_fold(
                args.nodes.split(","), args.id, args.label, args.spec, args.stage,
                topology=args.topology, scope=args.scope,
            ))
        elif args.cmd == "promote":
            print(op_promote(
                args.node, topology=args.topology, label=args.label, spec=args.spec, scope=args.scope,
            ))
        elif args.cmd == "unfold":
            attach = {}
            for pair in args.attach:
                neighbour, sep, child = pair.partition("=")
                if not sep or not neighbour.strip() or not child.strip():
                    raise ArchEngineError(f"--attach 格式应为 外部节点ID=子节点ID，收到：{pair}")
                attach[neighbour.strip()] = child.strip()
            print(op_unfold(args.node, attach, scope=args.scope))
        elif args.cmd == "verify":
            print(op_verify(args.fact, args.constraint, args.scope, args.source, args.supersedes))
        elif args.cmd == "set-node-state":
            print(op_set_node_state(
                args.node, args.impl, args.verify, args.verify_date,
                args.ref, args.add_ref, args.drop_ref, args.file_path,
            ))
        elif args.cmd == "open":
            if args.open_cmd == "add":
                print(op_open_add(args.question, args.kind, args.owner, args.scope, args.blocking))
            elif args.open_cmd == "resolve":
                print(op_open_resolve(args.id, args.resolution, args.constraint))
            else:
                print(op_open_list(args.node, args.scope, args.all, args.owner, args.blocking))
        elif args.cmd == "facts":
            print(op_facts(args.node, args.all, args.with_superseded))
        elif args.cmd == "status":
            print(op_status())
        elif args.cmd == "migrate-records":
            print(op_migrate_records())
        elif args.cmd == "graveyard":
            print(op_graveyard_add(args.node, args.scheme, args.reason))
        elif args.cmd == "rescope":
            print(op_rescope(args.id, args.scope))
        elif args.cmd == "legacy":
            print(op_legacy(args.legacy_cmd, getattr(args, "id", ""), getattr(args, "note", ""),
                            getattr(args, "all", False)))
        elif args.cmd == "config":
            print(op_config(
                global_fact_limit=args.global_fact_limit,
                preset=args.preset,
                default_topology=args.default_topology,
                max_depth=args.max_depth,
                max_chain_len=args.max_chain_len,
                max_hub_degree=args.max_hub_degree,
                max_cycle_len=args.max_cycle_len,
                max_active_holes=args.max_active_holes,
                max_unfolded_solid=args.max_unfolded_solid,
                auto_bridge_limit=args.auto_bridge_limit,
                allow_feedback=args.allow_feedback,
            ))
        elif args.cmd == "ground":
            print(op_ground(args.type, args.source, args.note))
        elif args.cmd == "export-task":
            print(op_export_task(args.node, scope=args.scope))
        elif args.cmd == "commit-node":
            print(op_commit_node(args.node, args.file_path, scope=args.scope))
        elif args.cmd == "sync-manual":
            print(op_sync_manual(args.node, args.summary, scope=args.scope))
        elif args.cmd == "reset-node":
            print(op_reset_node(args.node, args.reason, scope=args.scope))
        elif args.cmd == "fork":
            print(op_fork(args.from_tag))
    except ArchEngineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
