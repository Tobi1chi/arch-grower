"""Random op sequences against arch_engine in strict invariant mode.

Not part of the unittest suite (name has no test_ prefix). Run from the
repository root to measure how many structural violations the ops produce:

    python3 tests/fuzz_ops.py . 60 80      # <repo_root> <seeds> <steps>

Each finding lists the op, the violated invariant and the last few ops that
led there; seeds are deterministic, so a finding can be replayed.
"""
import json, os, random, sys, tempfile, traceback
from collections import Counter
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
SEEDS, STEPS = int(sys.argv[2]), int(sys.argv[3])
os.environ["ARCH_INVARIANTS"] = "strict"
sys.path.insert(0, str(repo / "scripts"))

import arch_cli  # noqa
from arch_engine import ArchEngineError, graph_ops, fold_ops, record_ops, common, kernel  # noqa
from arch_engine.invariants import validate_graph  # noqa

for mod in (graph_ops, fold_ops, record_ops, common, kernel):
    if hasattr(mod, "_shadow_commit"):
        mod._shadow_commit = lambda *a, **k: None
    if hasattr(mod, "_init_shadow_git"):
        mod._init_shadow_git = lambda *a, **k: None

KINDS = ["", "", "primary", "feedback", "bidirectional"]


def containers(g):
    out = [("", g["nodes"])]
    def walk(nodes):
        for n in nodes:
            if n["status"] == "super_node":
                out.append((n["id"], n["children"]))
                walk(n["children"])
    walk(g["nodes"])
    return out


def _all(g):
    st = list(g["nodes"])
    while st:
        n = st.pop(); yield n; st.extend(n.get("children") or [])


def run(seed):
    rng = random.Random(seed)
    findings = []
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        topo = rng.choice(["pipeline", "pipeline", "star", "ring"])
        arch_cli.op_init("fuzz", topology=topo) if topo != "pipeline" else arch_cli.op_init("fuzz")
        arch_cli.op_config(mode="advanced", max_depth=3, max_active_holes=50, max_chain_len=50,
                           max_hub_degree=50, max_cycle_len=50, max_unfolded_solid=50)
        counter = 0
        history = []
        for step in range(STEPS):
            g = json.loads(Path(".arch/graph.json").read_text())
            scope, nodes = rng.choice(containers(g))
            ids = [n["id"] for n in nodes]
            if not ids:
                continue
            pick = lambda: rng.choice(ids)
            counter += 1
            new_id = f"n{counter}"
            op = rng.choice(["add", "add", "collapse", "collapse", "collapse", "connect", "disconnect",
                             "deactivate", "activate", "fold", "fold", "fold", "unfold", "promote", "reset",
                             "contract"])
            try:
                if op == "add":
                    a, b = rng.choice([("", ""), (pick(), ""), ("", pick()), (pick(), pick())])
                    status = rng.choice(["hole", "hole", "fog"])
                    call = ("add_node", new_id, a, b, status)
                    arch_cli.op_add_node(new_id, f"? {new_id}", "s", status=status, after_node=a,
                                         before_node=b, edge_kind=rng.choice(KINDS), scope=scope)
                elif op == "collapse":
                    nid = pick(); call = ("collapse", nid)
                    arch_cli.op_collapse(nid, nid, "spec", scope=scope)
                elif op == "connect":
                    a, b = pick(), pick(); k = rng.choice(KINDS) or "primary"; call = ("connect", a, b, k)
                    arch_cli.op_connect(a, b, kind=k, scope=scope)
                elif op == "disconnect":
                    a, b = pick(), pick(); call = ("disconnect", a, b)
                    arch_cli.op_disconnect(a, b, scope=scope)
                elif op == "contract":
                    a, b = pick(), pick(); k = rng.choice(KINDS); call = ("set_edge_contract", a, b, k)
                    arch_cli.op_set_edge_contract(a, b, "C", kind=k, scope=scope)
                elif op == "deactivate":
                    nid = pick(); call = ("deactivate", nid)
                    arch_cli.op_deactivate_node(nid, scope=scope)
                elif op == "activate":
                    fogs = [n["id"] for n in nodes if n["status"] == "fog"]
                    if not fogs:
                        continue
                    nid = rng.choice(fogs)
                    a, b = rng.choice([(pick(), ""), ("", pick()), (pick(), pick())])
                    call = ("activate", nid, a, b)
                    arch_cli.op_activate_node(nid, after_node=a, before_node=b,
                                              edge_kind=rng.choice(KINDS), scope=scope)
                elif op == "fold":
                    solid = {n["id"] for n in nodes if n["status"] in ("solid", "super_node")}
                    cedges = next(c for c in [g] + [n for n in _all(g) if n.get("status") == "super_node"]
                                  if (c.get("id", "") == scope) or (not scope and c is g))
                    cedges = cedges.get("inner_edges", cedges.get("edges"))
                    if not solid:
                        continue
                    sel = [rng.choice(sorted(solid))]
                    for _ in range(rng.randint(1, 3)):
                        nb = [e["to"] if e["from"] in sel else e["from"] for e in cedges
                              if (e["from"] in sel) != (e["to"] in sel)]
                        nb = [x for x in nb if x in solid]
                        if nb:
                            sel.append(rng.choice(nb))
                    call = ("fold", sel, "S" + new_id)
                    arch_cli.op_fold(sel, "S" + new_id, "sub", "spec", scope=scope)
                elif op == "unfold":
                    sups = [n["id"] for n in nodes if n["status"] == "super_node"]
                    if not sups:
                        continue
                    nid = rng.choice(sups); call = ("unfold", nid)
                    arch_cli.op_unfold(nid, scope=scope)
                elif op == "promote":
                    nid = pick(); t = rng.choice(["", "pipeline", "star", "ring"]); call = ("promote", nid, t)
                    arch_cli.op_promote(nid, topology=t, scope=scope)
                elif op == "reset":
                    nid = pick(); call = ("reset", nid)
                    arch_cli.op_reset_node(nid, "r", scope=scope)
            except ArchEngineError as e:
                msg = str(e)
                if "GraphInvariant" in msg:
                    findings.append(("INVARIANT", seed, step, scope, call, msg.splitlines()[1:4], list(history)))
                continue
            except Exception as e:  # crash bug
                findings.append(("CRASH", seed, step, scope, call,
                                 [f"{type(e).__name__}: {e}", traceback.format_exc().splitlines()[-3]],
                                 list(history)))
                continue
            history.append((scope, call)); OK[call[0]] += 1
            # Soft violations (warnings) do not block a write; count them as findings too.
            for v in validate_graph(json.loads(Path(".arch/graph.json").read_text()), {"max_depth": 3}):
                if v.severity == "warning" and v.code != "I11":
                    findings.append(("WARNING", seed, step, scope, call, [str(v)], list(history)))
    return findings


OK = Counter()
all_f = []
ok_seeds = 0
for s in range(SEEDS):
    f = run(s)
    all_f += f
    ok_seeds += not f
sig = Counter()
examples = {}
for kind, seed, step, scope, call, msg, hist in all_f:
    key = (kind, call[0], msg[0][:140] if msg else "")
    import re
    key = (kind, call[0], re.sub(r"'[^']*'|\b[nS]n?\d+\b", "_", key[2]))
    sig[key] += 1
    examples.setdefault(key, (seed, step, scope, call, msg, hist))
print(f"seeds={SEEDS} steps={STEPS} clean_seeds={ok_seeds} findings={len(all_f)}")
for key, n in sig.most_common():
    seed, step, scope, call, msg, hist = examples[key]
    print(f"\n### {n}x {key[0]} in {key[1]}\n  e.g. seed={seed} step={step} scope={scope!r} call={call}")
    for m in msg:
        print("   ", m)
    print("   last ops:", hist[-6:])

print('successes:', dict(OK))
