"""Compare the compiled Foreman graph with the v3 blueprint without running it."""

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from inspect import getsourcefile, unwrap
import json
from pathlib import Path
import sys

import yaml
from langgraph.pregel import Pregel

from foreman import accept
from foreman.config import family, load_config
from foreman.delivery import FIXTURE_HUMAN, LiveRun, compile_delivery
from foreman.dry_run import DryRun
from foreman.outcomes import HALTS, Halt
from foreman.stages import STAGES

if __package__:
    from .gen import BLUEPRINT, ROOT, UniqueLoader, load_target
else:
    from gen import BLUEPRINT, ROOT, UniqueLoader, load_target


SENTINELS = {"__start__": {"kind": "node"}, "__end__": {"kind": "node"}}
# The migration slices in migration.md: M1-M7 (#232-#238), M8 Verify (#251) and the redesign (#299).
MIGRATION_ISSUES = (*range(232, 239), 251, 299)
DRAWN_EVENTS = {("issues", "labeled"): "issue_labeled"}


def module_inventory(source):
    modules = {}
    for path in sorted((source / "src/foreman").rglob("*.py")):
        text = path.read_text()
        symbols = {}

        def visit(item, prefix=""):
            if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + item.name
                start = min([item.lineno, *(decorator.lineno for decorator in item.decorator_list)])
                symbols[name] = {
                    "kind": "class" if isinstance(item, ast.ClassDef) else "function",
                    "line": start, "lines": item.end_lineno - start + 1,
                }
                for child in item.body:
                    visit(child, name + ".")
            else:
                for child in ast.iter_child_nodes(item):
                    visit(child, prefix)

        visit(ast.parse(text))
        modules[path.relative_to(source).as_posix()] = {
            "lines": len(text.splitlines()), "symbols": symbols,
        }
    return modules


def graph_snapshot(compiled):
    builder = compiled.builder
    nodes = {}
    for name, registration in builder.nodes.items():
        runnable = registration.runnable
        node = {"kind": "subgraph" if isinstance(runnable, Pregel) else "node"}
        # A bound step keeps its identity through functools.wraps, so the owner is the step, not the binding helper.
        function = unwrap(func) if (func := getattr(runnable, "func", None)) is not None else None
        if function is not None and (filename := getsourcefile(function)):
            path = Path(filename).resolve()
            if path.is_relative_to(ROOT):
                path = path.relative_to(ROOT)
            symbol = function.__qualname__.replace(".<locals>", "")
            node["owner"] = f"{path.as_posix()}::{symbol}"
        nodes[name] = node

    drawn = compiled.get_graph()
    nodes.update({name: node for name, node in SENTINELS.items() if name in drawn.nodes})
    # The drawable graph can coalesce aliases that share a destination.
    edges = [[origin, destination, None, False] for origin, destination in sorted(builder.edges)]
    edges.extend([list(origins), destination, None, False]
                 for origins, destination in sorted(builder.waiting_edges))
    open_routes = []
    for origin, branches in sorted(builder.branches.items()):
        for name, branch in sorted(branches.items()):
            if branch.ends is None:
                open_routes.append([origin, name])
            else:
                edges.extend([origin, destination, route, True] for route, destination in branch.ends.items())
    for origin, registration in builder.nodes.items():
        ends = registration.ends
        if isinstance(ends, dict):
            edges.extend([origin, destination, label, True] for destination, label in ends.items())
        else:
            edges.extend([origin, destination, destination, True] for destination in ends or ())
    pairs = {(origin, destination) for origin, destination, _, _ in edges if isinstance(origin, str)}
    inferred = [[edge.source, edge.target, edge.data, edge.conditional] for edge in drawn.edges
                if (edge.source, edge.target) not in pairs]
    metadata = (compiled.config or {}).get("metadata", {})
    return {
        "nodes": nodes, "edges": edges, "inferred_edges": inferred, "open_routes": open_routes,
        "declarations": metadata.get("stage_declarations", {}),
        "contract_id": metadata.get("contract_id"),
        "subgraphs": {name: graph_snapshot(child) for name, child in compiled.get_subgraphs()},
    }


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def project():
    config = load_config()
    dry = DryRun()

    def forbidden_dispatch(*_):
        raise AssertionError("The projector must never invoke a worker")

    compiled = compile_delivery(forbidden_dispatch, config, run=LiveRun(forge=dry, workspace=dry, human_login=FIXTURE_HUMAN))
    procedures = {}
    for name, stage in STAGES.items():
        selector = getattr(stage, "model_selector", name)
        policy = config.nodes.get(selector)
        dependency = getattr(stage, "model_dependency", policy.other_family_from if policy else None)
        skills = []
        for rule in stage.skills:
            method = rule.skill
            if isinstance(method, str):
                skills.append({"ref": f"src/foreman/skills/{method}.md", "when": rule.when or "always",
                               "kind": "repository_skill", "lines": None})
            else:
                skills.append({"ref": method.path, "when": rule.when or "always", "kind": method.kind,
                               "lines": None})
        output = stage.output
        prompt = None
        if stage.prompt:
            path = f"src/foreman/stages/{stage.prompt}"
            prompt = {"path": path, "sha256": sha256((ROOT / path).read_bytes()).hexdigest()}
        procedure = {
            "kind": stage.kind, "worker": getattr(stage, "worker", policy.role if policy else None),
            "step": getattr(stage, "step", None),
            "model_policy": f"nodes.{selector}" if policy else None, "model_dependency": dependency,
            "inputs": sorted(stage.input_fields),
            "prompt": prompt,
            "skills": skills, "output": f"{output.__module__}.{output.__name__}" if output else None,
            "output_fields": sorted(output.model_fields) if output else [],
        }
        if policy and stage.kind == "model":
            examples = [{}]
            if dependency in config.nodes:
                examples = [{dependency: model.name} for model in config.nodes[dependency].models]
            procedure["model_orders"] = [
                {"models_used": used, "order": [model.name for model in stage.models(name, {"models_used": used}, config)]}
                for used in examples
            ]
            expected_orders = []
            for used in examples:
                contributor_families = {family(model) for model in used.values()}
                ordered = sorted(policy.models, key=lambda model: model.family in contributor_families)
                expected_orders.append([model.name for model in ordered])
            if expected_orders != [example["order"] for example in procedure["model_orders"]]:
                procedure["policy_orders"] = expected_orders
        procedures[name] = procedure

    # Probe every pair of GitHub event and action literals with a synthetic delivery that adds Ready to an issue with no run.
    intake_source = ast.parse(Path(accept.__file__).read_text())
    literals = sorted({node.value for node in ast.walk(intake_source)
                       if isinstance(node, ast.Constant) and isinstance(node.value, str)})
    delivery = {"label": {"name": "Ready"}, "issue": {"number": 1}, "repository": {"full_name": "example/synthetic"}, "comment": {}}
    started = [(name, action) for name in literals for action in literals
               if (decision := accept.accept_event(name, {**delivery, "action": action}, lambda repo, issue: None)) and decision.action == "start"]
    return {
        "schema_version": 1,
        # The drawing names GitHub's issues/labeled delivery as Warp's issue_labeled trigger.
        "intake": [DRAWN_EVENTS.get(pair, ".".join(pair)) for pair in started],
        "graph": graph_snapshot(compiled),
        "procedures": procedures,
        "modules": module_inventory(ROOT),
        "policies": {f"nodes.{name}": [model.name for model in row.models] for name, row in config.nodes.items()},
        "pstack_revision": config.pstack.commit,
        "halts": {halt.value: HALTS[halt].recovery if halt in HALTS else None for halt in set(Halt) | HALTS.keys()},
    }


@dataclass(frozen=True)
class Difference:
    level: str
    scope: str
    subject: str
    expected: object
    observed: object

    @property
    def kind(self):
        if self.observed is None:
            return "missing"
        return "extra" if self.expected is None else "mismatched"

    @property
    def id(self):
        data = [self.level, self.scope, self.subject, self.expected, self.observed]
        return "V2-" + digest(json.dumps(data, sort_keys=True))[:16]

    def __str__(self):
        return (f"{self.id} {self.kind} [{self.level}/{self.scope}] {self.subject}: "
                f"{self.observed!r} -> {self.expected!r}")


def compare(target, actual):
    differences = []

    def record(level, scope, subject, expected, observed):
        if expected != observed:
            differences.append(Difference(level, scope, subject, expected, observed))

    def graph_diff(level, scope, nodes, transitions, snapshot):
        current = snapshot.get("nodes", {})
        for name in sorted(nodes.keys() | current.keys()):
            expected, observed = nodes.get(name), current.get(name)
            if expected and observed and "owner" not in expected and observed["kind"] == expected["kind"]:
                observed = {"kind": observed["kind"]}
            record(level, scope, f"node {name}", expected, observed)

        edges = []
        expected_transitions = ([origin, destination, route, route is not None]
                                for origin, destination, route in transitions)
        for transitions_at_side in (expected_transitions, snapshot.get("edges", [])):
            grouped = defaultdict(list)
            for origin, destination, route, conditional in transitions_at_side:
                pair = (tuple(origin) if isinstance(origin, list) else origin, destination)
                grouped[pair].append([conditional, route])
            edges.append(grouped)
        expected_edges, actual_edges = edges
        for origin, destination in sorted(expected_edges.keys() | actual_edges.keys(), key=repr):
            pair = (origin, destination)
            record(level, scope, f"edge {origin} -> {destination}",
                   sorted(expected_edges[pair], key=repr) or None, sorted(actual_edges[pair], key=repr) or None)
        for origin, branch in snapshot.get("open_routes", []):
            record(level, scope, f"unmapped branch {origin}.{branch}", None, True)
        for origin, destination, route, conditional in snapshot.get("inferred_edges", []):
            record("shared", scope, f"unregistered route {origin} -> {destination}", None, [conditional, route])

    event = target["intake"]["event"]
    for name in sorted(set(actual["intake"]) | {event}):
        record("ingress", "triage", f"admit {name}", True if name == event else None,
               True if name in actual["intake"] else None)

    stages = target["stages"]
    root_edges = [["__start__", target["entry"], None]]
    for name, contract in stages.items():
        root_edges.extend([name, destination, route] for route, destination in contract["exits"].items())
    graph = actual["graph"]
    graph_diff("top", "delivery", {**SENTINELS, **{name: {"kind": "subgraph"} for name in stages}},
               root_edges, graph)

    def compare_contract(scope, contract, snapshot):
        implementation = contract["implementation"]
        module = implementation["module"]
        nodes = {step: {"kind": "node", "owner": f"{module}::{row['owner']}"}
                 for step, row in contract["steps"].items()}
        if snapshot:
            nodes.update(SENTINELS)
        else:
            record("steps", scope, "subgraph endpoints", sorted(SENTINELS), None)
        graph_diff("steps", scope, nodes, contract["transitions"], snapshot)
        failure = contract.get("failure_and_resume", contract)
        for field in ("halts", "retries"):
            record("steps", scope, field, failure[field], snapshot.get("declarations", {}).get(field))

    dispatches = {}
    for name, contract in stages.items():
        snapshot = graph["subgraphs"].get(name, {})
        compare_contract(name, contract, snapshot)
        for step, child in snapshot.get("subgraphs", {}).items():
            caller = f"{name}.{step}"
            if child["contract_id"] == "dispatch":
                dispatches[caller] = child
            else:
                record("steps", caller, "unexpected nested subgraph", None, child["contract_id"] or step)

    dispatch = target["dispatch"]
    callers = set(dispatch["callers"])
    for caller in sorted(callers | dispatches.keys()):
        record("steps", "dispatch", f"call from {caller}", True if caller in callers else None,
               True if caller in dispatches else None)
    if not dispatches:
        compare_contract("dispatch", dispatch, {})
    for caller, snapshot in sorted(dispatches.items()):
        compare_contract(caller, dispatch, snapshot)
        for name in snapshot["subgraphs"]:
            record("steps", caller, f"unexpected nested subgraph {name}", None, True)

    for name, contract in {**stages, "dispatch": dispatch}.items():
        implementation = contract["implementation"]
        module = implementation["module"]
        owner = actual["modules"].get(module, {})
        expected_symbols = {implementation["entry"], *(row["owner"] for row in contract["steps"].values())}
        symbols = owner.get("symbols", {})
        owners = {symbol for symbol, span in symbols.items()
                  if span["kind"] == "function" and "." not in symbol}
        for symbol in sorted(expected_symbols | owners):
            record("steps", name, f"owner {module}::{symbol}",
                   symbol if symbol in expected_symbols else None,
                   symbol if symbols.get(symbol, {}).get("kind") == "function" else None)
        limit = implementation["max_lines"]
        if owner and owner["lines"] > limit:
            record("steps", name, f"line ceiling for {module}", limit, owner["lines"])

    expected_procedures = target["procedures"]
    observed_procedures = actual["procedures"]
    for name in sorted(expected_procedures.keys() | observed_procedures.keys()):
        procedure = expected_procedures.get(name)
        observed = observed_procedures.get(name)
        if procedure is None or observed is None:
            record("procedure", name, "declaration", procedure, observed)
            continue
        contract = stages[procedure["step"].split(".")[0]]
        output = procedure.get("output", contract["outputs"])
        instructions = procedure.get("instructions", contract["instructions"])
        expected = {
            "step": procedure["step"], "kind": "model", "worker": procedure["role"],
            "model_policy": procedure["model_policy"], "model_dependency": procedure.get("model_dependency"),
            "inputs": sorted(procedure["inputs"]), "output": output["schema"],
            "output_fields": sorted(output["fields"]),
            "prompt": {"path": instructions["runtime_prompt"],
                       "sha256": sha256((ROOT / instructions["prompt"]).read_bytes()).hexdigest()},
            "skills": [{key: skill.get(key) for key in ("ref", "when", "kind", "lines")}
                       for skill in procedure.get("skills", contract["skills"])],
        }
        for field, value in expected.items():
            record("procedure", name, field, value, observed.get(field))
        if "policy_orders" in observed:
            record("procedure", name, "model order", observed["policy_orders"],
                   [example["order"] for example in observed["model_orders"]])

    halts = {halt for contract in stages.values() for halt in contract["failure_and_resume"]["halts"]}
    halts.update(dispatch["halts"])
    registered_halts = actual["halts"]
    for halt in sorted(halts | registered_halts.keys()):
        record("shared", "halts", halt, True if halt in halts else None,
               True if halt in registered_halts else None)
        if halt in registered_halts and registered_halts[halt] is None:
            record("shared", "halts", f"policy for {halt}", True, None)
    record("shared", "dispatch", "pstack pin", target["repositories"]["pstack"]["revision"],
           actual["pstack_revision"])
    return sorted(differences, key=lambda row: (row.kind, row.level, row.scope, row.subject))


def check(differences, waivers):
    allowed = {}
    errors = []
    for issue, entries in waivers.items():
        if type(issue) is not int or issue not in MIGRATION_ISSUES:
            errors.append(f"Waiver group {issue!r} must name a migration issue: {', '.join(f'#{n}' for n in MIGRATION_ISSUES)}")
        if not isinstance(entries, dict):
            errors.append(f"Waiver group #{issue} must map difference IDs to subjects")
            continue
        for key, subject in entries.items():
            if key in allowed:
                errors.append(f"Duplicate waiver {key}")
            allowed[key] = (issue, subject)
    observed = {row.id for row in differences}
    errors.extend(f"Unwaived: {row}" for row in differences if row.id not in allowed)
    for key in sorted(allowed.keys() - observed):
        issue, subject = allowed[key]
        errors.append(f"Stale waiver {key} (#{issue}): {subject}")
    return errors


def load_waivers():
    return yaml.load((BLUEPRINT / "waivers.yaml").read_text(), Loader=UniqueLoader)["waivers"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the actual projection to this YAML file")
    parser.add_argument("--check", action="store_true", help="Fail on unwaived differences or stale waivers")
    args = parser.parse_args()
    actual = project()
    if args.output:
        args.output.write_text(yaml.safe_dump(json.loads(json.dumps(actual)), sort_keys=True))
    differences = compare(load_target(), actual)
    if args.check:
        errors = check(differences, load_waivers())
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print(f"Blueprint projection matches with {len(differences)} waived differences.")
    else:
        for row in differences:
            print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
