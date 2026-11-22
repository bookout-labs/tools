"""Render the review index from blueprint records and pinned file metadata."""

from html import escape
from pathlib import PurePosixPath
import re


def text(value):
    return escape(str(value), quote=True)


def diagram(name, title):
    return f"""<figure class="diagram" aria-label="{text(title)} diagram">
  <div class="diagram-tools" aria-label="Diagram controls">
    <button data-view="out" type="button" aria-label="Zoom out">−</button>
    <button data-view="in" type="button" aria-label="Zoom in">+</button>
    <button data-view="reset" type="button">Fit</button>
    <span>Drag to pan · Scroll or pinch to zoom</span>
  </div>
  <div class="canvas" tabindex="0" aria-label="{text(title)}. Arrow keys pan, plus and minus zoom.">
    <img src="views/{text(name)}.svg" draggable="false" alt="{text(title)} flow diagram" />
  </div>
</figure>"""


def file_card(path, file):
    parts = file["repository"].split("/") + path.split("/")
    branch = "\n".join("    " * depth + ("└── " if depth else "") + part
                       for depth, part in enumerate(parts))
    suffix = " (baseline)" if file["kind"] == "baseline prompt" else ""
    name = text(PurePosixPath(path).name + suffix)
    link = f'<a href="{text(file["url"])}">{name}</a>' if "url" in file else f'<span>{name}</span>'
    summary = "".join(f"<dt>{text(key)}</dt><dd>{text(value)}</dd>"
                      for key, value in file["summary"].items() if value)
    description = text(file["description"] or
                       ("Planned owner. File absent at the baseline." if "url" not in file
                        else "Description missing from the source file."))
    return f"""<li class="file-chip">{link}
      <details class="file-details">
        <summary aria-label="Details for {name}" title="Details for {name}">ⓘ</summary>
        <div class="file-pop">
          <span class="kind">{text(file["kind"])}</span>
          <pre>{text(branch)}</pre><p>{description}</p><dl>{summary}</dl>
        </div>
      </details>
    </li>"""


def list_items(items):
    return "".join(f"<li>{text(item)}</li>" for item in items)


def stage_page(name, stage, files, paths, decisions, gate_rule):
    warp = stage["warp"]
    steps = "".join(f"<li><strong>{text(row['action'])}</strong><p>{text(row['result'])}</p></li>"
                    for row in stage["steps"].values())
    gates = list_items(f"{gate['answerer']} answers “{gate['question']}.” "
                       f"{gate_rule} Resume {gate['resume'].replace('_', ' ')} to verify the answer."
                       for gate in stage.get("gates", []))
    recovery = stage.get("failure_and_resume", {}).get("recovery")
    contract = f"<p>{text(stage['scope'])}</p>" if "scope" in stage else ""
    if recovery:
        contract += f"<p>{text(recovery)}</p>"
    outputs = stage.get("outputs", {})
    if "merge_binding" in outputs:
        contract += (f"<p>{text(outputs['merge_binding'])} "
                     f'<a href="{text(outputs["source"])}">GitHub PR API ↗</a></p>')
    scenarios = "".join(f"<li><strong>{text(case['given'])}</strong><p>{text(case['expect'])}</p></li>"
                        for case in stage["scenarios"].values())
    choices = "".join(f'<a href="#decisions">{text(decisions[key])}</a>'
                      for key in stage.get("decisions", []))
    path = name if name == "dispatch" else "stages/" + name
    panel = "".join(file_card(path, files[path]) for path in paths)
    return f"""<section class="page" id="{text(name)}">
      <header class="page-head"><h1>{text(stage["title"])}</h1><p>{text(stage["purpose"])}</p></header>
      {diagram(path, stage["title"])}
      <section class="file-panel"><h2>Stage files</h2>
      <ul class="file-list">{panel}</ul></section>
      <div class="columns"><section><h2>What happens</h2><ol class="steps">{steps}</ol></section>
      <section><h2>Warp equivalent</h2><p><strong>{text(warp["equivalent"])}</strong></p>
      <p>{text(warp["divergence"])}</p><p><a href="{text(warp["page"])}">Read Warp’s documentation ↗</a></p>
      {f'<h2>When Joe answers</h2><ul>{gates}</ul>' if gates else ''}</section></div>
      <details class="reference"><summary>Scope, recovery and checks</summary>
        {contract}<ul>{list_items(stage["invariants"])}</ul>
        <h3>Cases the implementation must pass</h3><ul class="steps">{scenarios}</ul>
        <p class="decision-links">{choices}</p>
        <a href="target.yaml">Read the full stage contract</a>
      </details>
    </section>"""


def render_inline(value):
    parts = re.split(r"\[([^\]]+)\]\((https://[^)]+)\)", value)
    result = [text(parts[0])]
    for i in range(1, len(parts), 3):
        label, url, following = parts[i:i + 3]
        result.append(f'<a href="{text(url)}">{text(label)} ↗</a>{text(following)}')
    return "".join(result)


def page(directory, target, files, sections):
    decisions = (directory / "decisions.md").read_text()
    agreed = decisions.split("## Agreed direction\n", 1)[1].split("\n## D1.", 1)[0].strip()
    agreement, _, source = agreed.rpartition("\n\n")
    agreement_lines = agreement.splitlines()
    agreement_items = list_items(line[2:] for line in agreement_lines if line.startswith("- "))
    headings = dict(re.findall(r"^## (D\d+)\. (.*)$", decisions, re.M))
    cards = []
    for block in re.split(r"\n## (?=D\d+\.)", decisions)[1:]:
        heading, _, body = block.partition("\n")
        cards.append(f"<article><h2>{text(heading)}</h2><p>{render_inline(body.strip())}</p></article>")

    stages = target["stages"]
    stage_links = "".join(f'<a href="#{text(key)}">{text(key.title())}</a>' for key in stages)
    worker_links = "".join(f'<a href="#worker-{text(key)}">{text(key.title())}</a>' for key in target["workers"])
    gate_rule = target["rules"]["gates"]["continuation"]
    stage_pages = "".join(stage_page(name, stage, files, sections[name], headings, gate_rule)
                          for name, stage in {**stages, "dispatch": target["dispatch"]}.items())
    worker_pages = []
    for name, worker in target["workers"].items():
        procedure_text = []
        for procedure in target["procedures"].values():
            if procedure["role"] != name:
                continue
            stage_name = procedure["step"].split(".")[0]
            stage = stages[stage_name]
            instructions = procedure.get("instructions", stage["instructions"])
            skills = procedure.get("skills", stage["skills"])
            skill_text = list_items(f"{skill['method']} Applied {skill['when'].replace('_', ' ')}." for skill in skills)
            prompt = instructions["prompt"]
            prompt_file = (directory / files[prompt]["url"]).read_text()
            _, _, prompt_body = prompt_file.partition("\n\n")
            procedure_text.append(f"""<li><a href="#{text(stage_name)}">{text(stage["title"])}</a>
            <p><a href="{text(files[prompt]["url"])}">Target prompt</a>
            <span> · Model policy <code>{text(procedure["model_policy"])}</code></span></p>
            <details><summary>Complete worker instructions</summary>
              <p>{text(prompt_body)}</p></details>
            {f'<ul>{skill_text}</ul>' if skill_text else ''}</li>""")
        worker_pages.append(f"""<section class="page" id="worker-{text(name)}">
        <header class="page-head"><h1>{text(worker["title"])}</h1><p>{text(worker["method"])}</p></header>
        {diagram("workers/" + name, worker["title"])}<div class="columns"><section>
        <h2>Assignments</h2><ul class="steps">{"".join(procedure_text)}</ul></section>
        <section><h2>Permissions</h2><p>{text(worker["permissions"])}</p>
        <h2>Conversation</h2><p>{text(worker["conversation"])}</p></section></div></section>""")
    file_rows = []
    for path, file in files.items():
        full = text(f"{file['repository']}/{path}")
        title = f'<a href="{text(file["url"])}">{full}</a>' if "url" in file else full
        detail = text(file["description"] or "Source description unavailable")
        file_rows.append(f"<li><code>{title}</code><p>{detail}</p></li>")
    css = (directory / "page.css").read_text()
    script = (directory / "page.js").read_text()
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>Foreman blueprint · v{target["blueprint_version"]}</title>
<style>{css}</style></head>
<body>
<div class="shell"><aside><a class="brand" href="#overview">Foreman <small>Blueprint v{target["blueprint_version"]}</small></a>
<nav aria-label="Blueprint navigation"><div class="nav-group">
<a href="#overview">Overview</a><a href="#context">System context</a><a href="#decisions">Decisions for Joe</a>
</div><h2>Stages</h2><div class="nav-group">{stage_links}<a href="#dispatch">Shared dispatch</a></div>
<h2>Workers</h2><div class="nav-group">{worker_links}</div>
<h2>Reference</h2><div class="nav-group"><a href="#files">All file paths</a>
<a href="target.yaml">Blueprint YAML</a><a href="glossary.md">Glossary</a><a href="code-map.md">Code map</a>
<a href="verification.md">Verification</a><a href="migration.md">Migration</a></div></nav></aside>
<main>
<section class="page" id="overview"><header class="page-head"><h1>From issue to merged pull request</h1>
<p>{text(target["overview"]["framing"])} <a href="{text(target["overview"]["source"])}">Notion’s book ↗</a></p></header>
{diagram("target", "Foreman workflow")}
<p class="legend"><span>▰ Stage</span><span>▱ Step</span><span>◉ Worker call</span><span>◇ Question gate</span><span>○ Result</span></p>
<p class="legend">{text(gate_rule)} If a valid result has no matching route, Foreman opens the stage's question gate and records a routing defect for the scorers.</p>
<div class="stage-grid">{"".join(f'<a href="#{key}"><strong>{text(stage["title"])}</strong><span>{text(stage["purpose"])}</span></a>' for key, stage in stages.items())}</div>
<p class="baseline">{text(target["status"].capitalize())} blueprint · Baseline <a href="https://github.com/{text(target["repositories"]["foreman_repository"])}/commit/{text(target["baseline"])}"><code>{text(target["baseline"][:8])}</code></a></p>
</section>
<section class="page" id="context"><header class="page-head"><h1>Where Foreman works</h1>
<p>Foreman runs from bookout-labs/tools. Each issue names the repository where its changes belong.</p></header>
{diagram("context", "Repository and authority boundaries")}
<p>The GitHub Project board shows when an issue is Ready, Doing, Done or needs an answer. GitHub, the CARR record layer and T3 hold shared context. Git versions the blueprint.</p></section>
{stage_pages}{"".join(worker_pages)}
<section class="page" id="decisions"><header class="page-head"><h1>Decisions for Joe</h1>
<p>Agreed decisions record Joe’s rulings. Open decisions include a recommendation.</p></header>
<section class="reference"><h2>Agreed direction</h2><p>{text(agreement_lines[0])}</p>
<ul>{agreement_items}</ul><p>{render_inline(source)}</p></section>
<div class="decision-list">{"".join(cards)}</div></section>
<section class="page" id="files"><header class="page-head"><h1>All file paths</h1>
<p>Target prompts open from this package. Baseline source links open the pinned GitHub revision. Planned owner paths become links when the files exist.</p></header>
<p><a href="files.txt">Plain-text index</a> · <a href="files.yaml">File metadata</a> · <a href="missing-descriptions.txt">Missing descriptions</a></p>
<ul class="all-files">{"".join(file_rows)}</ul></section>
</main></div><script>{script}</script>
</body></html>
"""
