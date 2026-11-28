"""Generate Foreman's review pages and static diagrams from the target blueprint."""

import argparse
import ast
from collections import defaultdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import mkdtemp
from urllib.parse import quote

import xml.etree.ElementTree as ET

import yaml

BLUEPRINT = Path(__file__).resolve().parent
ROOT = BLUEPRINT.parents[2]
PROMPT_DIR = BLUEPRINT.relative_to(ROOT) / "prompts"
FOREMAN_SOURCE = "src/foreman/"
SENTINELS = {"__start__", "__end__"}
REQUIRED = {
    "title", "purpose", "scope", "entry", "worker", "model_policy", "instructions",
    "skills", "inputs", "outputs", "steps", "transitions", "exits", "gates", "effects",
    "invariants", "failure_and_resume", "implementation", "checks", "decisions", "scenarios", "warp",
}


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"Duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_target():
    target = yaml.load((BLUEPRINT / "target.yaml").read_text(), Loader=UniqueLoader)
    stages = target["stages"]
    if target["entry"] not in stages:
        raise ValueError("The entry must name a stage")
    for name, contract in {**stages, "dispatch": target["dispatch"]}.items():
        if name in stages and set(contract) != REQUIRED:
            raise ValueError(f"{name} stage fields differ: {set(contract) ^ REQUIRED}")
        steps = contract["steps"]
        valid = set(steps) | SENTINELS
        transitions = contract["transitions"]
        if len({tuple(edge) for edge in transitions}) != len(transitions):
            raise ValueError(f"Duplicate transition in {name}")
        route_keys = [(origin, route) for origin, _, route in transitions if route is not None]
        if len(set(route_keys)) != len(route_keys):
            raise ValueError(f"{name} routes a typed value more than once from a step")
        for origin, destination, _ in transitions:
            if origin not in valid or destination not in valid:
                raise ValueError(f"{name} has an unknown transition endpoint")
        reached = {"__start__"}
        for _ in valid:
            reached.update({destination for origin, destination, _ in transitions if origin in reached})
        if reached != valid:
            raise ValueError(f"{name} has unreachable steps: {valid - reached}")
        paused = set(contract.get("failure_and_resume", contract).get("waits", {}))
        paused.update(gate["resume"] for gate in contract.get("gates", []))
        if paused - steps.keys():
            raise ValueError(f"{name} has an unknown continuation")
        if set(contract["checks"]) != set(contract["scenarios"]):
            raise ValueError(f"{name} checks and scenario IDs differ")
        for step, row in steps.items():
            if set(row) != {"actor", "action", "result", "owner", "check"} or row["check"] not in contract["checks"]:
                raise ValueError(f"{name}.{step} has an invalid step contract")
            if re.search(r"\b(this repo|here|there|it|handle|process|as needed|appropriate|etc|and/or|normal|small|large|done|approved|same|retry|new)\b", row["action"], re.I):
                raise ValueError(f"{name}.{step} action needs a specific subject")
        for route, destination in contract.get("exits", {}).items():
            if destination != "__end__" and destination not in stages:
                raise ValueError(f"{name}.{route} targets {destination}, an unknown stage")
            if destination == "__end__" and route not in target["terminal_outcomes"]:
                raise ValueError(f"{name}.{route} is an undeclared terminal result")
    worker_steps = {step for worker in target["workers"].values() for step in worker["steps"]}
    procedure_steps = {procedure["step"] for procedure in target["procedures"].values()}
    if worker_steps != procedure_steps or procedure_steps != set(target["dispatch"]["callers"]):
        raise ValueError("Worker, procedure and dispatch assignments disagree")
    for procedure in target["procedures"].values():
        stage, step = procedure["step"].split(".")
        if step not in stages[stage]["steps"]:
            raise ValueError(f"Unregistered procedure step {procedure['step']}")
        instruction = procedure.get("instructions", stages[stage]["instructions"])
        prompt = ROOT / instruction["prompt"]
        if prompt.parent != BLUEPRINT / "prompts" or not prompt.is_file() or not instruction["runtime_prompt"].startswith("src/foreman/stages/"):
            raise ValueError(f"{procedure['step']} must declare a target and runtime prompt")
    return target


def mtext(value):
    return (str(value).replace("&", "&amp;").replace('"', "#quot;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("|", "#124;").replace("\n", "<br/>"))


def label(value):
    names = {
        "P0": "Critical defect",
        "P1": "Serious defect",
        "none": "No blocking finding",
        "changes": "Blocking finding",
        "validated_single": "One slice clears both checks",
        "validated_parent": "Parent plan clears both checks",
        "validated_batch": "Feature children filed",
        "head_moved": "PR revision changed",
        "owned_head_moved": "Owned head moved",
        "reviewable_head": "Checked revision",
        "halted": "Run stops",
        "foreign_repository": "Wrong repository",
        "exhausted": "Plan revision limit reached",
    }
    return names.get(value, value.replace("_", " ").strip().capitalize())


def mermaid(nodes, edges, direction="TD"):
    shapes = {"stage": ('["', '"]'), "worker": ('(["', '"])'),
              "gate": ('{{"', '"}}'), "terminal": ('(["', '"])'), "step": ('["', '"]')}
    ids = {name: f"n{index}" for index, name in enumerate(nodes)}
    lines = [f"flowchart {direction}"]
    for name, (title, kind) in nodes.items():
        opening, closing = shapes[kind]
        lines.append(f"    {ids[name]}{opening}{mtext(title)}{closing}:::{kind}")
    destinations = defaultdict(list)
    for origin, destination, detail in edges:
        if detail:
            destinations[origin, destination].append(detail)
        else:
            destinations.setdefault((origin, destination), [])
    for (origin, destination), details in destinations.items():
        annotation = origin.startswith(("gate_", "wait_")) or destination.startswith(("gate_", "wait_"))
        arrow = "-.->" if annotation else "-->"
        text = f'|"{mtext(" / ".join(dict.fromkeys(details)))}"|' if details else ""
        lines.append(f"    {ids[origin]} {arrow}{text} {ids[destination]}")
    lines.extend([
        "    classDef stage fill:#dcebf8,stroke:#315675,color:#10283b",
        "    classDef step fill:#edf3f8,stroke:#597187,color:#132638",
        "    classDef worker fill:#d7f0e8,stroke:#357369,color:#133c36",
        "    classDef gate fill:#fff0c9,stroke:#89641f,color:#3f2b0b",
        "    classDef terminal fill:#f0f0f0,stroke:#666666,color:#202020",
    ])
    return "\n".join(lines) + "\n"


def stage_links(name, contract, target, nodes):
    exits = contract["exits"]
    normalize = target["rules"]["exit_normalization"]
    for origin, destination, route in contract["transitions"]:
        if destination != "__end__":
            yield origin, destination, label(route or "")
            continue
        for outcome in (route,) if route else exits:
            normalized = normalize.get(outcome, outcome)
            if normalized not in exits:
                raise ValueError(f"{name}.{outcome} has no declared exit destination")
            next_stage = exits[normalized]
            if next_stage == "__end__":
                end = f"exit_{normalized}"
                nodes[end] = (f"{label(normalized)}\nReported on the issue", "terminal")
                detail = label(outcome) if outcome != normalized else ""
            else:
                end = f"continue_{next_stage}"
                nodes[end] = (f"Continue to {next_stage.title()} stage", "stage")
                detail = label(outcome)
            yield origin, end, detail


def views(target):
    out = {}
    context = target["context"]
    nodes = dict.fromkeys(name for origin, destination, _ in context for name in (origin, destination))
    out["context"] = mermaid({name: (name, "stage") for name in nodes}, context)
    stages = target["stages"]
    intake = target["intake"]
    nodes = {"entry": (intake["trigger"], "terminal")}
    nodes.update({name: (name.title() + " · " + value["title"], "stage") for name, value in stages.items()})
    links = [("entry", target["entry"], intake["delivery"])]
    for name, stage in stages.items():
        for route, destination in stage["exits"].items():
            if destination == "__end__":
                destination = route
                nodes[destination] = ("Halted" if route == "halted" else label(route), "terminal")
                route = ""
            links.append((name, destination, label(route)))
    out["target"] = mermaid(nodes, links)
    callers = set(target["dispatch"]["callers"])
    for name, stage in {**stages, "dispatch": target["dispatch"]}.items():
        gates = stage.get("gates", [])
        waits = stage.get("failure_and_resume", stage).get("waits", {})
        nodes = {}
        entry_label = intake["trigger"] if name == target["entry"] else "Enter stage"
        nodes["__start__"] = (entry_label, "terminal")
        for step, row in stage["steps"].items():
            kind = "worker" if f"{name}.{step}" in callers else "step"
            nodes[step] = (f"{row['actor']} · {row['action']}", kind)
        if name in stages:
            links = list(stage_links(name, stage, target, nodes))
        else:
            links = []
            for origin, destination, route in stage["transitions"]:
                if destination == "__end__":
                    destination = f"exit_{route}"
                    nodes[destination] = (label(route), "terminal")
                    route = None
                links.append((origin, destination, label(route or "")))
        if name == target["entry"]:
            links = [
                (origin, destination, "Ready label, dependent recheck or revert issue"
                 if origin == "__start__" and destination in stage["steps"] else detail)
                for origin, destination, detail in links
            ]
        for gate in gates:
            step = gate["resume"]
            gate_id = f"gate_{step}"
            title = "\n".join([gate["answerer"], *(f"• {bullet}" for bullet in gate["bullets"])])
            nodes[gate_id] = (title, "gate")
            route = gate.get("route", "Unresolved choice")
            links.extend([(step, gate_id, route.replace("_", " ")), (gate_id, step, "")])
        for step, purpose in waits.items():
            wait_id = f"wait_{step}"
            nodes[wait_id] = (purpose, "step")
            links.extend([(step, wait_id, "Pending"), (wait_id, step, "Relevant event")])
        out[f"stages/{name}" if name in stages else name] = mermaid(nodes, links)
    for name, worker in target["workers"].items():
        nodes = {name: (worker["title"], "worker")}
        links = []
        for step in worker["steps"]:
            stage, node = step.split(".")
            nodes[step] = (stages[stage]["steps"][node]["action"], "step")
            links.append((name, step, "Assignment"))
        out[f"workers/{name}"] = mermaid(nodes, links, "LR")
    return out


def write_views(drawings, svgs, output):
    view_dir = output / "views"
    for name, content in drawings.items():
        path = view_dir / f"{name}.mmd"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if svgs is not None:
        for name, content in svgs.items():
            (view_dir / f"{name}.svg").write_text(content)
        paths = sorted(f"{name}.{extension}" for name in drawings for extension in ("mmd", "svg"))
        (view_dir / "SHA256SUMS").write_text("".join(
            f"{sha256((view_dir / path).read_bytes()).hexdigest()}  {path}\n" for path in paths
        ))


def render(drawings, puppeteer):
    scratch = Path(mkdtemp(prefix=".scratch.", dir=ROOT))
    manifest = {
        "drawings": drawings,
        "mermaidConfig": json.loads((BLUEPRINT / "mermaid.json").read_text()),
        "puppeteerConfig": json.loads(puppeteer.read_text()) if puppeteer else {},
        "output": str(scratch),
    }
    env = dict(os.environ, PUPPETEER_SKIP_DOWNLOAD="true",
               FOREMAN_RENDER_SCRIPT=str(BLUEPRINT / "render.mjs"))
    command = ["npx", "-y", "--package=@mermaid-js/mermaid-cli@11.4.2", "-c",
               'node "$FOREMAN_RENDER_SCRIPT" "$(command -v mmdc)"']
    subprocess.run(command, env=env, input=json.dumps(manifest), text=True, check=True)
    svgs = {}
    for index, name in enumerate(drawings, start=1):
        rendered = (scratch / f"rendered-{index}.svg").read_text()
        root = ET.fromstring(rendered)
        if root.tag != "{http://www.w3.org/2000/svg}svg" or not root.get("viewBox"):
            raise ValueError(f"Invalid diagram output for {name}")
        _, _, width, height = root.attrib["viewBox"].split()
        rendered = rendered.replace('width="100%"', f'width="{width}" height="{height}"', 1)
        svgs[name] = rendered.rstrip() + "\n"
    return svgs


def baseline_file(repo, revision, path):
    result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=repo,
                            check=False, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def inventory(target, pins):
    """List every referenced file. `pins` maps each skill repository name to a checkout holding its pinned commit."""
    specs = defaultdict(list)
    sections = defaultdict(set)
    planned = set()
    target_prompts, runtime_prompts = set(), set()
    origins = {}
    subprocess.run(["git", "cat-file", "-e", f"{target['baseline']}^{{commit}}"],
                   cwd=ROOT, check=True)
    for name, checkout in pins.items():
        revision = target["repositories"][name]["revision"]
        actual_pin = subprocess.check_output(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
                                             cwd=checkout, text=True).strip()
        if actual_pin != revision:
            raise ValueError(f"Pinned {name} commit is unavailable")

    def add_skills(section, skills):
        for skill in skills:
            ref, name = skill["ref"], skill.get("repository", "pstack")
            if name == "target_repository":  # Lives in each target checkout, so it has no pinned source.
                continue
            path = ref if ref.startswith(FOREMAN_SOURCE) else f"{target['repositories'][name]['directory']}/{ref}"
            if not ref.startswith(FOREMAN_SOURCE):
                origins[path] = name
            specs[path].append(skill)
            sections[section].add(path)

    for section, file_paths in target["source_map"].items():
        sections[section].update(file_paths)
    for section, contract in {**target["stages"], "dispatch": target["dispatch"]}.items():
        owner = contract["implementation"]["module"]
        sections[section].add(owner)
        if section != "dispatch":
            planned.add(owner)
            if baseline_file(ROOT, target["baseline"], owner) is None:
                # A stage absent at the baseline also has its runtime prompt and checks planned. Stages built at the baseline stay strict.
                planned.update([*contract["checks"].values(), contract.get("instructions", {}).get("runtime_prompt", owner)])
        sections[section].update(contract["checks"].values())
        add_skills(section, contract.get("skills", []))
    for procedure in target["procedures"].values():
        stage = procedure["step"].split(".")[0]
        instructions = procedure.get("instructions", target["stages"][stage]["instructions"])
        prompt, runtime_prompt = instructions["prompt"], instructions["runtime_prompt"]
        target_prompts.add(prompt)
        runtime_prompts.add(runtime_prompt)
        if target["stages"][stage]["instructions"].get("runtime_prompt") in planned:
            # A procedure added to a stage that is absent at the baseline has its runtime prompt planned too.
            planned.add(runtime_prompt)
        sections[stage].update([prompt, runtime_prompt, "src/foreman/models.toml"])
        add_skills(stage, procedure.get("skills", []))

    files = {}
    for path in sorted(set().union(*sections.values())):
        origin = target["repositories"].get(origins.get(path, ""), {})
        external = path in origins
        repository = origin["repository"] if external else target["repositories"]["foreman_repository"]
        revision = origin["revision"] if external else target["baseline"]
        if path in target_prompts:
            entry = describe(path, (ROOT / path).read_text(), ())
            entry.update(kind="target prompt", repository=repository, revision="target",
                         url=Path(path).relative_to(BLUEPRINT.relative_to(ROOT)).as_posix())
            files[path] = entry
            continue
        source = baseline_file(pins[origins[path]] if external else ROOT, revision, path)
        if source is None and path not in planned:
            raise ValueError(f"Missing pinned source: {path}")
        entry = describe(path, source, specs[path])
        if path in runtime_prompts:
            entry["kind"] = "baseline prompt"
        entry.update({"repository": repository, "revision": revision})
        if source is not None:
            entry["url"] = f"https://github.com/{repository}/blob/{revision}/{quote(path)}"
        files[path] = entry
    return files, {section: sorted(paths) for section, paths in sections.items()}


def describe(path, source, skills):
    if source is None:
        return {"kind": "planned script", "description": "", "summary": {}}
    lines = source.splitlines()
    top = source.strip()
    description, summary = "", {}
    if path.endswith(".py"):
        kind = "test" if path.startswith("tests/") else "script"
        tree = ast.parse(source)
        description = ast.get_docstring(tree) or ""
        if kind == "test":
            tests = [node.name.removeprefix("test_").replace("_", " ")
                     for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
            summary["What it checks"] = " • ".join(tests[:5])
        else:
            functions = [node for node in tree.body
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")]
            if not functions:
                functions = [method for node in tree.body if isinstance(node, ast.ClassDef)
                             for method in node.body if isinstance(method, ast.FunctionDef) and not method.name.startswith("_")]
            functions = functions[:3]
            summary["What it does"] = " • ".join((ast.get_docstring(node) or node.name).split("\n")[0] for node in functions)
            summary["Inputs"] = " • ".join(f"{node.name}({ast.unparse(node.args)})" for node in functions)
            summary["Outputs"] = " • ".join(f"{node.name} → {ast.unparse(node.returns)}" for node in functions if node.returns)
    elif path.endswith("SKILL.md"):
        kind = "skill"
        if lines and lines[0] == "---":
            metadata = yaml.safe_load("\n".join(lines[1:lines.index("---", 1)]))
            description = metadata.get("description", "")
    elif path.endswith(".md"):
        kind = "skill" if skills else "prompt" if "/stages/" in path or Path(path).parent == PROMPT_DIR else "document"
        if top.startswith("# "):
            description = top.splitlines()[0][2:]
        if kind == "prompt":
            description = description or top.splitlines()[0]
            paragraphs = [p for p in top.split("\n\n") if not p.startswith(("#", "```", "---"))]
            summary["What it instructs"] = paragraphs[0] if paragraphs else ""
    else:
        kind = "script" if path.endswith(".sh") else "config" if path.endswith((".toml", ".yaml", ".yml")) else "diagram"
        description = next((line.lstrip("# ").strip() for line in lines[:4] if line.startswith(("# ", "## "))), "")
        if kind == "script":
            summary["What it does"] = description
    if skills:
        for skill in skills:
            # A skill without a line range loads whole.
            start, end = skill["lines"] or (1, len(lines))
            if not 1 <= start <= end <= len(lines):
                raise ValueError(f"Skill excerpt is out of range: {path}:{start}–{end}")
            heading = skill.get("heading")
            if heading and f"# {heading}" not in lines[:start]:
                raise ValueError(f"Skill heading absent: {path}: {heading}")
        summary["Method steps"] = " • ".join(
            f"{skill['method']} ({'whole file' if not skill['lines'] else 'lines {}–{}'.format(*skill['lines'])})"
            for skill in skills)
    return {"kind": kind, "description": " ".join(description.split()), "summary": summary}


def write_maps(target, files, output):
    file_lines, missing = [], []
    for path, file in files.items():
        full_path = f"{file['repository']}/{path}"
        file_lines.append(full_path + (f"  —  {file['description']}" if file["description"] else ""))
        if not file["description"]:
            missing.append(full_path)
    (output / "files.txt").write_text("\n".join(file_lines) + "\n")
    (output / "files.yaml").write_text(yaml.safe_dump(files, sort_keys=False, allow_unicode=True))
    (output / "missing-descriptions.txt").write_text("\n".join(missing) + ("\n" if missing else ""))
    lines = ["# Foreman code map", "", "Paths are relative to the Foreman repository. Planned owners may be absent at the pinned baseline.", ""]
    for name, contract in {**target["stages"], "dispatch": target["dispatch"]}.items():
        owner = contract["implementation"]
        lines.extend([
            f"## {label(name)}", "", contract.get("scope", contract["purpose"]), "",
            f"Entry `{owner['module']}::{owner['entry']}`. Maximum {owner['max_lines']} physical lines in the owner module.", "",
            "Never " + ", ".join(owner["never_does"]) + ".", "",
            "Uses " + ", ".join(owner["shared"]) + ".", "",
            "| Step | Owner symbol | Scenario |", "| --- | --- | --- |",
        ])
        for step, row in contract["steps"].items():
            lines.append(f"| {name}.{step} | `{owner['module']}::{row['owner']}` | {row['check']} |")
        baseline_links = [f"[`{path}`]({files[path]['url']})" for path in target["source_map"][name]]
        lines.extend(["", "Baseline files " + ", ".join(baseline_links) + ".", ""])
    (output / "code-map.md").write_text("\n".join(lines))


def main():
    from page import page

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pstack", type=Path, required=True, help="Git repository containing the pinned pstack commit")
    parser.add_argument("--matt-skills", type=Path, required=True, help="Git repository containing the pinned mattpocock/skills commit")
    parser.add_argument("--output", type=Path, default=BLUEPRINT, help="Output directory (defaults to the blueprint directory)")
    rendering = parser.add_mutually_exclusive_group()
    rendering.add_argument("--no-svg", action="store_true", help="Generate text and Mermaid source without a browser")
    rendering.add_argument("--puppeteer", type=Path, help="Mermaid CLI browser configuration for rendering SVGs")
    args = parser.parse_args()
    target = load_target()
    files, sections = inventory(target, {"pstack": args.pstack, "matt_skills": args.matt_skills})
    drawings = views(target)
    svgs = None if args.no_svg else render(drawings, args.puppeteer)
    write_views(drawings, svgs, args.output)
    write_maps(target, files, args.output)
    (args.output / "index.html").write_text(page(BLUEPRINT, target, files, sections))


if __name__ == "__main__":
    main()
