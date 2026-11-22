import tests

import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from foreman.stages import STAGES
from foreman.config import PSTACK_PIN
from foreman.skill_rules import PACKAGE_SKILLS, PstackMethod, export_skills

SPAWN_INSTRUCTIONS = re.compile(r"\b(?:subagents?|spawn\w*|delegat\w*|arena|task\s+tool)\b", re.IGNORECASE)


class SkillRulesTest(unittest.TestCase):
    def test_line_ranges_are_positive_and_ordered(self):
        for lines in ((0, 1), (2, 1)):
            with self.subTest(lines=lines), self.assertRaises(ValidationError):
                PstackMethod(path="method/SKILL.md", kind="skill", lines=lines)

    @unittest.skipUnless(os.environ.get("FOREMAN_PINNED_SKILL_REPO"), "requires a local checkout with the pstack pin")
    def test_pinned_methods_have_no_spawn_instructions(self):
        source = Path(os.environ["FOREMAN_PINNED_SKILL_REPO"]).resolve()
        pin = PSTACK_PIN.model_copy(update={"repository": source.as_uri()})
        with TemporaryDirectory() as scratch:
            root = export_skills(pin, Path(scratch))
            for stage, row in STAGES.items():
                for rule in row.skills:
                    method = rule.skill
                    path = root / method.path if isinstance(method, PstackMethod) else PACKAGE_SKILLS / f"{method}.md"
                    with self.subTest(stage=stage, path=path.name):
                        content = path.read_text(encoding="utf-8")
                        if isinstance(method, PstackMethod) and method.lines:
                            start, end = method.lines
                            content = "\n".join(content.splitlines()[start - 1:end])
                        self.assertTrue(content.strip())
                        self.assertNotRegex(content, SPAWN_INSTRUCTIONS, str(path))
