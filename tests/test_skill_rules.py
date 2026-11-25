import tests

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from foreman.stages import STAGES
from foreman.config import MATT_SKILLS_PIN, PSTACK_PIN
from foreman.dry_run import DryRun
from foreman.skill_rules import PACKAGE_SKILLS, export_skills, stage_methods
from foreman.state import PlanState
from skill_fixture import skill_fixture


class SkillRulesTest(unittest.TestCase):
    def test_methods_load_whole_skill_files(self):
        """RE4: a pinned method is its whole file, with no line range."""
        with skill_fixture() as (config, scratch):
            root = export_skills(config.pstack, config.matt_skills, scratch)
            for stage, expected in (
                ("research", {"how/SKILL.md": root / "how/SKILL.md", "why/SKILL.md": root / "why/SKILL.md",
                              "engineering/diagnosing-bugs/SKILL.md":
                                  root / "matt_skills/engineering/diagnosing-bugs/SKILL.md"}),
                ("review", {"src/foreman/skills/review-quality.md": PACKAGE_SKILLS / "review-quality.md",
                            "blast-radius/SKILL.md": root / "blast-radius/SKILL.md"}),
            ):
                with self.subTest(stage=stage):
                    instructions, methods, _ = stage_methods(stage, {"plan": PlanState(work_kind="bug_fix")}, root, DryRun())
                    self.assertEqual(methods, list(expected))
                    self.assertEqual(instructions, "\n\n".join(
                        f"Read {path} in full and follow it." for path in expected.values()))

    @unittest.skipUnless(os.environ.get("FOREMAN_PINNED_SKILL_REPO") and os.environ.get("FOREMAN_PINNED_MATT_SKILLS"),
                         "requires local checkouts with the pstack and mattpocock/skills pins")
    def test_pinned_methods_exist_and_are_not_empty(self):
        pins = [pin.model_copy(update={"repository": Path(os.environ[variable]).resolve().as_uri()}) for pin, variable in (
            (PSTACK_PIN, "FOREMAN_PINNED_SKILL_REPO"), (MATT_SKILLS_PIN, "FOREMAN_PINNED_MATT_SKILLS"))]
        with TemporaryDirectory() as scratch:
            root = export_skills(*pins, Path(scratch))
            for stage, row in STAGES.items():
                for rule in row.skills:
                    method = rule.skill
                    if method.kind == "target_skill":
                        continue  # The target repository supplies its own skill, not the pin.
                    path = method.source(root)
                    with self.subTest(stage=stage, path=path.name):
                        self.assertTrue(path.read_text(encoding="utf-8").strip())
