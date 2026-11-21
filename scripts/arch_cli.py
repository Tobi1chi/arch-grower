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
    ArchEngineError, op_activate_node, op_add_node, op_collapse, op_commit_node, op_config,
    op_deactivate_node, op_export_task, op_export_ui, op_facts, op_focus, op_fold, op_fork,
    op_ground, op_init, op_legacy, op_migrate_records, op_open_add, op_open_list,
    op_open_resolve, op_rescope, op_reset_node, op_set_edge_contract, op_set_node_state,
    op_status, op_sync_manual, op_unfold, op_verify,
)
from arch_engine.common import REF_WHOLE_FILE_LIMIT, _load_graph, _save_graph, _today
from arch_engine.graph import (
    IMPL_LEVELS, SIGNATURE_LINE, VERIFY_LEVELS, _find_any_node, _find_walking_skeleton,
    _node_state,
)
from arch_engine.records import (
    DEFAULT_TITLES, GENERATED_MARKER, GT_LINE, GY_LINE, MD_HEADING, OPEN_KINDS, OPEN_OWNERS,
    _global_fact_budget, _read_records, _save_records,
)
from arch_engine.refs import _extract_md_section

# Everything importable from arch_cli before the engine moved into arch_engine/
# stays importable from here (tests and ad-hoc scripts rely on it).
__all__ = [
    "main", "ArchEngineError",
    "op_activate_node", "op_add_node", "op_collapse", "op_commit_node", "op_config",
    "op_deactivate_node", "op_export_task", "op_export_ui", "op_facts", "op_focus", "op_fold",
    "op_fork", "op_ground", "op_init", "op_legacy", "op_migrate_records", "op_open_add",
    "op_open_list", "op_open_resolve", "op_rescope", "op_reset_node", "op_set_edge_contract",
    "op_set_node_state", "op_status", "op_sync_manual", "op_unfold", "op_verify",
    "REF_WHOLE_FILE_LIMIT", "_load_graph", "_save_graph", "_today",
    "SIGNATURE_LINE", "_find_any_node", "_find_walking_skeleton", "_node_state",
    "DEFAULT_TITLES", "GENERATED_MARKER", "GT_LINE", "GY_LINE", "MD_HEADING",
    "_global_fact_budget", "_read_records", "_save_records", "_extract_md_section",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Autoregressive Architecture State Machine CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--seed", required=True)

    p_focus = sub.add_parser("focus")
    p_focus.add_argument("--node", default=None)

    p_col = sub.add_parser("collapse")
    p_col.add_argument("--node", required=True)
    p_col.add_argument("--label", required=True)
    p_col.add_argument("--spec", required=True)
    p_col.add_argument("--file-path", default="")
    p_col.add_argument("--in-contract", default="")
    p_col.add_argument("--out-contract", default="")
    p_col.add_argument("--type", default="port")
    p_col.add_argument("--stage", default="")

    p_edge = sub.add_parser("set-edge-contract", help="更新指定连线的契约")
    p_edge.add_argument("--from", dest="from_node", required=True)
    p_edge.add_argument("--to", dest="to_node", required=True)
    p_edge.add_argument("--contract", required=True)

    p_add = sub.add_parser("add-node")
    p_add.add_argument("--node", required=True)
    p_add.add_argument("--label", required=True)
    p_add.add_argument("--spec", required=True)
    p_add.add_argument("--status", default="hole", choices=["hole", "solid", "fog"])
    p_add.add_argument("--type", default="port")
    p_add.add_argument("--stage", default="")
    p_add.add_argument("--after", default="")
    p_add.add_argument("--before", default="")
    p_add.add_argument("--contract", default="TBD")

    p_exp_ui = sub.add_parser("export-ui", help="导出 Stage 泳道交互式架构看板 HTML")
    p_exp_ui.add_argument("--out", default="")

    p_deact = sub.add_parser("deactivate", help="将活跃节点下线并移入战争迷雾区，自动缝合上下游边")
    p_deact.add_argument("--node", required=True)
    p_deact.add_argument("--reason", default="")
    p_deact.add_argument("--bypass-contract", default="")

    p_act = sub.add_parser("activate", help="将迷雾区节点激活为待定空洞并接入主干")
    p_act.add_argument("--node", required=True)
    p_act.add_argument("--after", default="")
    p_act.add_argument("--before", default="")
    p_act.add_argument("--contract", default="TBD")

    p_fold = sub.add_parser("fold", help="将已确定、相互连通的子图折叠为一个超级节点")
    p_fold.add_argument("--nodes", required=True, help="逗号分隔的待折叠节点 ID，如 a,b,c")
    p_fold.add_argument("--id", required=True, help="超级节点 ID")
    p_fold.add_argument("--label", required=True)
    p_fold.add_argument("--spec", required=True)
    p_fold.add_argument("--stage", default="")

    p_unfold = sub.add_parser("unfold", help="展开超级节点，恢复其子节点与连线")
    p_unfold.add_argument("--node", required=True)
    p_unfold.add_argument(
        "--attach", action="append", default=[], metavar="NEIGHBOUR=CHILD",
        help="折叠期间新增连线的归位：外部节点ID=子节点ID（可重复）",
    )

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

    sub.add_parser("status", help="项目进度总览：节点实现/验证、待办项、记录统计")
    sub.add_parser("migrate-records", help="把旧格式 ground_truth.md / graveyard.md 导入 .arch/records.json（幂等）")

    p_rescope = sub.add_parser("rescope", help="修改事实 (GT-xx) 或待办项 (OI-xx) 的作用域")
    p_rescope.add_argument("--id", required=True)
    p_rescope.add_argument("--scope", action="append", required=True)

    p_legacy = sub.add_parser("legacy", help="未分类遗留记录：list / show / resolve")
    legacy_sub = p_legacy.add_subparsers(dest="legacy_cmd", required=True)
    p_llist = legacy_sub.add_parser("list")
    p_llist.add_argument("--all", action="store_true", help="包括已处理的")
    p_lshow = legacy_sub.add_parser("show")
    p_lshow.add_argument("--id", required=True)
    p_lres = legacy_sub.add_parser("resolve")
    p_lres.add_argument("--id", required=True)
    p_lres.add_argument("--note", default="", help="处理去向，如“拆为 GT-12、OI-03”")

    p_cfg = sub.add_parser("config", help="查看或设置记录配置")
    p_cfg.add_argument("--global-fact-limit", type=int, default=None, help="global 事实字数上限（默认 3000）")

    p_gnd = sub.add_parser("ground")
    p_gnd.add_argument("--type", required=True, choices=["human_verify", "doc_ref", "probe_cmd", "code_ref"])
    p_gnd.add_argument("--source", required=True)
    p_gnd.add_argument("--note", default="")

    p_exp = sub.add_parser("export-task")
    p_exp.add_argument("--node", required=True)

    p_com = sub.add_parser("commit-node")
    p_com.add_argument("--node", required=True)
    p_com.add_argument("--file-path", required=True, action="append", help="实现文件，可重复绑定多个")

    p_syn = sub.add_parser("sync-manual")
    p_syn.add_argument("--node", required=True)
    p_syn.add_argument("--summary", required=True)

    p_rst = sub.add_parser("reset-node")
    p_rst.add_argument("--node", required=True)
    p_rst.add_argument("--reason", required=True)

    p_frk = sub.add_parser("fork")
    p_frk.add_argument("--from-tag", required=True)

    args = parser.parse_args()
    try:
        if args.cmd == "init":
            print(op_init(args.seed))
        elif args.cmd == "focus":
            print(op_focus(args.node))
        elif args.cmd == "collapse":
            print(op_collapse(
                args.node, args.label, args.spec,
                args.file_path, args.in_contract, args.out_contract, args.type, args.stage
            ))
        elif args.cmd == "set-edge-contract":
            print(op_set_edge_contract(args.from_node, args.to_node, args.contract))
        elif args.cmd == "add-node":
            print(op_add_node(
                args.node, args.label, args.spec,
                args.status, args.type, args.stage, args.after, args.before, args.contract
            ))
        elif args.cmd == "export-ui":
            print(op_export_ui(args.out))
        elif args.cmd == "deactivate":
            print(op_deactivate_node(args.node, args.reason, args.bypass_contract))
        elif args.cmd == "activate":
            print(op_activate_node(args.node, args.after, args.before, args.contract))
        elif args.cmd == "fold":
            print(op_fold(args.nodes.split(","), args.id, args.label, args.spec, args.stage))
        elif args.cmd == "unfold":
            attach = {}
            for pair in args.attach:
                neighbour, sep, child = pair.partition("=")
                if not sep or not neighbour.strip() or not child.strip():
                    raise ArchEngineError(f"--attach 格式应为 外部节点ID=子节点ID，收到：{pair}")
                attach[neighbour.strip()] = child.strip()
            print(op_unfold(args.node, attach))
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
        elif args.cmd == "rescope":
            print(op_rescope(args.id, args.scope))
        elif args.cmd == "legacy":
            print(op_legacy(args.legacy_cmd, getattr(args, "id", ""), getattr(args, "note", ""),
                            getattr(args, "all", False)))
        elif args.cmd == "config":
            print(op_config(args.global_fact_limit))
        elif args.cmd == "ground":
            print(op_ground(args.type, args.source, args.note))
        elif args.cmd == "export-task":
            print(op_export_task(args.node))
        elif args.cmd == "commit-node":
            print(op_commit_node(args.node, args.file_path))
        elif args.cmd == "sync-manual":
            print(op_sync_manual(args.node, args.summary))
        elif args.cmd == "reset-node":
            print(op_reset_node(args.node, args.reason))
        elif args.cmd == "fork":
            print(op_fork(args.from_tag))
    except ArchEngineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
