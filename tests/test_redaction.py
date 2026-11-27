"""Redaction behavior and the permitted output boundaries."""
import tests

import ast
import json
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from importlib.metadata import version
from io import StringIO
from pathlib import Path
from re import Pattern
import subprocess
import unittest
from unittest.mock import patch

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import default_settings, get_plugins

from foreman.cli import main
from foreman.delivery import run_delivery
from foreman.github import LiveForge
from foreman.redaction import redact, register_test_values
from foreman.stage_output import load_stored_slice as _load_slice
from foreman.stage_output import StoredSlice


# Synthetic credential shapes assembled at runtime. None have been issued.
SYNTHETIC_GITHUB = "ghp_" + "a" * 36
SYNTHETIC_HEX = "08bf3e67af5d91cb" * 4
SYNTHETIC_SHA = SYNTHETIC_HEX[:40]
SYNTHETIC_JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJzeW50aGV0aWMifQ" + "." + "a" * 43

SYNTHETIC_CASES = {
    "Artifactory Credentials": ("AKC" + "a" * 12, '"{}"'),
    "AWS Access Key": ("AKIA" + "F" * 16, "{}"),
    "Azure Storage Account access key": ("AccountKey=" + "a" * 86 + "==", "{}"),
    "Base64 High Entropy String": ("AbCdEfGhIjKlMnOpQrStUvWxYz01234567-_", '"{}"'),
    "Basic Auth Credentials": ("synthetic-pass", "https://user:{}@example.invalid"),
    "Cloudant Credentials": ("a" * 24, 'cloudant_apikey="{}"'),
    "Discord Bot Token": ("M" + "a" * 23 + "." + "a" * 6 + "." + "a" * 27, "{}"),
    "GitHub Token": (SYNTHETIC_GITHUB, "{}"),
    "GitLab Token": ("glpat-" + "a" * 20, "{}"),
    "Hex High Entropy String": (SYNTHETIC_HEX, '"{}"'),
    "IBM COS HMAC Credentials": (SYNTHETIC_HEX[:48], 'cos_hmac_secret_access_key="{}"'),
    "IBM Cloud IAM Key": ("a" * 44, 'ibm_cloud_iam_api_key="{}"'),
    "JSON Web Token": (SYNTHETIC_JWT, "{}"),
    "Mailchimp Access Key": ("a" * 32 + "-us99", "{}"),
    "NPM tokens": ("npm_" + "a" * 36, "//example.invalid/:_authToken={}"),
    "OpenAI Token": ("sk-" + "a" * 20 + "T3BlbkFJ" + "a" * 20, "{}"),
    "Private Key": (
        "-----BEGIN " + "PRIVATE KEY-----\nsynthetic key material\n-----END " + "PRIVATE KEY-----",
        "{}",
    ),
    "Public IP (ipv4)": ("203.0.113.7", "{}"),
    "PyPI Token": ("pypi-" + "AgEIcHlwaS5vcmc" + "a" * 70, "{}"),
    "Secret Keyword": ("synthetic-pass", 'password = "{}"'),
    "SendGrid API Key": ("SG." + "a" * 22 + "." + "a" * 43, "{}"),
    "Slack Token": ("xoxb-" + "1234-5678-synthetic", "{}"),
    "SoftLayer Credentials": ("a" * 64, 'softlayer_api_key="{}"'),
    "Square OAuth Secret": ("sq0csp-" + "a" * 43, "{}"),
    "Stripe Access Key": ("sk_live_" + "a" * 24, "{}"),
    "Telegram Bot Token": ("123456789:" + "a" * 35, "{}"),
    "Twilio API Key": ("SK" + "a" * 32, "{}"),
}


class RedactionTest(unittest.TestCase):
    def test_a_registered_test_value_is_removed_exactly_from_every_later_text_line_by_line(self):
        """DQ4: each test value the run supplied, matched as written, which no pattern would find, and each line of one that
        spans lines, because check output is redacted as it streams, one line at a time."""
        single, multiline = "plainvalue-7d41", "first-line-90a2\nsecond-line-55c3\n"
        self.assertEqual(redact(f"token {single}"), f"token {single}")
        register_test_values([single, multiline, "   "])
        self.assertEqual(redact(f"token {single} and {single}."), "token [redacted:Test Value] and [redacted:Test Value].")
        self.assertEqual(redact("a second-line-55c3 b\n"), "a [redacted:Test Value] b\n")
        self.assertEqual(redact(f"x={single}", credentials_only=True), "x=[redacted:Test Value]")
        # A blank value registers nothing, so text stays as written.
        self.assertEqual(redact("a   b"), "a   b")

    def test_pinned_plugins_expose_the_pattern_data_foreman_uses(self):
        self.assertEqual(version("detect-secrets"), "1.5.0")
        without_denylist = {"Base64 High Entropy String", "Hex High Entropy String", "Secret Keyword"}
        with default_settings():
            self.assertEqual({plugin.secret_type for plugin in get_plugins()}, SYNTHETIC_CASES.keys())
            for plugin in get_plugins():
                if plugin.secret_type in without_denylist:
                    continue
                with self.subTest(kind=plugin.secret_type):
                    patterns = getattr(plugin, "denylist", ())
                    self.assertTrue(patterns, "The pinned detector no longer exposes its denylist.")
                    self.assertTrue(all(isinstance(pattern, Pattern) for pattern in patterns))

    @patch("foreman.github.subprocess.run")
    def test_issue_write_redacts_every_plugin_kind_inside_a_slice_field(self, gh):
        gh.return_value = subprocess.CompletedProcess([], 0, '{"number":42}', "")
        for kind, (secret, surrounding) in SYNTHETIC_CASES.items():
            with self.subTest(kind=kind):
                child = StoredSlice(
                    title="Child issue", acceptance=f'Before \\ "{surrounding.format(secret)}"\nAfter',
                    depends_on=[], screen="", properties=[],
                )
                title = f"Synthetic case {SYNTHETIC_GITHUB}"
                self.assertEqual(LiveForge().create_issue("Example/Target", title, "", ["slice"], child=child, marker=""), 42)
                argv = gh.call_args.args[0]
                self.assertIn("title=Synthetic case [redacted:GitHub Token]", argv)
                body = next(arg.removeprefix("body=") for arg in argv if arg.startswith("body="))
                acceptance = _load_slice(body).acceptance
                assert isinstance(acceptance, str)
                self.assertIn(f"[redacted:{kind}]", acceptance)
                self.assertNotIn(secret, acceptance)
                self.assertNotIn(secret, "\n".join(argv))

    @patch("foreman.github.subprocess.run")
    def test_write_redacts_labels_and_ignores_source_allowlist_comments(self, gh):
        gh.return_value = subprocess.CompletedProcess([], 0, '{"number":42}', "")
        body = f"{SYNTHETIC_GITHUB} # pragma: allowlist secret"
        forge = LiveForge()
        forge.create_issue("Example/Target", "Synthetic fixture", body, [SYNTHETIC_GITHUB], marker="")
        self.assertIn("body=[redacted:GitHub Token] # pragma: allowlist secret", gh.call_args.args[0])
        self.assertIn("labels[]=[redacted:GitHub Token]", gh.call_args.args[0])
        forge.edit_labels("Example/Target", 42, [SYNTHETIC_GITHUB], [])
        self.assertEqual(gh.call_args.args[0][-1], "[redacted:GitHub Token]")

    def test_keyword_redacts_only_the_assigned_synthetic_value(self):
        self.assertEqual(
            redact('Synthetic abc before password="abc" and abc after'),
            'Synthetic abc before password="[redacted:Secret Keyword]" and abc after',
        )

    @patch("foreman.github.subprocess.run")
    def test_issue_write_redacts_repeated_keyword_matches_on_one_line(self, gh):
        gh.return_value = subprocess.CompletedProcess([], 0, '{"number":42}', "")
        body = 'password="synthetic-aaaa" password="synthetic-bbbb"'
        LiveForge().create_issue("Example/Target", "Synthetic fixture", body, [], marker="")
        self.assertIn(
            'body=password="[redacted:Secret Keyword]" password="[redacted:Secret Keyword]"',
            gh.call_args.args[0],
        )

    @patch("foreman.github.subprocess.run")
    def test_redaction_preserves_the_slice_json_delimiters(self, gh):
        gh.return_value = subprocess.CompletedProcess([], 0, '{"number":42}', "")
        token = "npm_" + "a" * 36
        child = StoredSlice.model_validate({
            "title": f"Explain BEGIN PRIVATE KEY in the docs {SYNTHETIC_GITHUB}",
            "acceptance": f"//example.invalid/:_authToken={token}",
            "files": [f"src/{SYNTHETIC_GITHUB}.py"],
            "screen": f"docs/screens/{SYNTHETIC_GITHUB}.md",
            "depends_on": [f"Setup {SYNTHETIC_GITHUB}"],
            "properties": [{
                "name": "-----BEGIN " + 'PRIVATE KEY-----\nsynthetic "key" material',
                "constraint": f"row_check {SYNTHETIC_GITHUB}",
            }],
        })
        LiveForge().create_issue("Example/Target", child.title, f"Excerpt {SYNTHETIC_GITHUB}", ["slice"], child=child, marker="")
        written = next(arg for arg in gh.call_args.args[0] if arg.startswith("body="))
        self.assertIn("body=Excerpt [redacted:GitHub Token]\n", written)
        self.assertEqual(_load_slice(written.removeprefix("body=")).model_dump(exclude_unset=True), {
            "title": "Explain [redacted:Private Key] in the docs [redacted:GitHub Token]",
            "acceptance": "[redacted:NPM tokens]",
            "files": ["src/[redacted:GitHub Token].py"],
            "screen": "docs/screens/[redacted:GitHub Token].md",
            "depends_on": ["Setup [redacted:GitHub Token]"],
            "properties": [{"name": "[redacted:Private Key]", "constraint": "row_check [redacted:GitHub Token]"}],
        })

    def test_upstream_jwt_plugin_omits_the_synthetic_signature(self):
        with default_settings():
            values = {hit.secret_value for hit in scan_line(SYNTHETIC_JWT) if hit.type == "JSON Web Token"}
        self.assertTrue(values)
        self.assertNotIn(SYNTHETIC_JWT, values, "Delete the JWT extension now that detect-secrets reports the signature.")
        self.assertEqual(redact(SYNTHETIC_JWT), "[redacted:JSON Web Token]")

    def test_upstream_private_key_plugin_omits_the_synthetic_block(self):
        block, _ = SYNTHETIC_CASES["Private Key"]
        with default_settings():
            values = {hit.secret_value for hit in scan_line(block) if hit.type == "Private Key"}
        self.assertTrue(values)
        self.assertNotIn(block, values, "Delete the private-key extension now that detect-secrets reports the block.")
        self.assertEqual(redact(block), "[redacted:Private Key]")

    def test_unterminated_private_key_stops_at_the_first_blank_line(self):
        block = "-----BEGIN " + "PRIVATE KEY-----\nx\n\nDependencies:\nNone"
        self.assertEqual(redact(block), "[redacted:Private Key]\n\nDependencies:\nNone")

    @patch("foreman.github.subprocess.run")
    def test_commit_sha_is_preserved_in_live_and_dry_merge_arguments(self, gh):
        from test_merge_receipt import OBSERVATION, SNAPSHOT, QUEUED, REPO, BRANCH
        observation = OBSERVATION.model_copy(deep=True)
        observation.pr.head.sha = SYNTHETIC_SHA
        before = {**SNAPSHOT, "headRefOid": SYNTHETIC_SHA}
        after = {**QUEUED, "headRefOid": SYNTHETIC_SHA,
                 "mergeQueueEntry": QUEUED["mergeQueueEntry"]}
        gh.side_effect = [subprocess.CompletedProcess([], 0, json.dumps(before), ""),
                          subprocess.CompletedProcess([], 0, "", ""),
                          subprocess.CompletedProcess([], 0, json.dumps(after), "")]
        LiveForge().arm_auto_merge(REPO, 82, BRANCH, SYNTHETIC_SHA, observation)
        live = next(call.args[0] for call in gh.call_args_list if call.args[0][1:3] == ["pr", "merge"])
        self.assertEqual(live[-2:], ["--match-head-commit", SYNTHETIC_SHA])
        gh.side_effect = None
        gh.return_value = subprocess.CompletedProcess([], 0, '{"number":42}', "")
        LiveForge().create_issue("Example/Target", "Synthetic fixture", SYNTHETIC_SHA, [], marker="")
        self.assertIn("body=[redacted:Hex High Entropy String]", gh.call_args.args[0])

    def test_run_log_redacts_dry_run_check_and_live_error(self):
        base = ["run", "delivery", "--repo", "Example/Target"]
        # The dry run's contract fixture names a check that prints a token.
        contract = f"check: echo {SYNTHETIC_GITHUB}\nverify_skill: .claude/skills/verify-web/SKILL.md\nimage: node:22\nsetup: []\nsecrets: []\n".encode()
        with redirect_stdout(StringIO()) as stdout, patch("foreman.cli.run_delivery", partial(run_delivery, contract=contract)):
            code = main([*base, "--dry-run", "--request", "Add a field"])
        self.assertEqual(code, 0)
        self.assertIn("check echo [redacted:GitHub Token]\n", stdout.getvalue())
        self.assertIn("done merge_pending\n", stdout.getvalue())
        self.assertNotIn(SYNTHETIC_GITHUB, stdout.getvalue())

        with (
            patch("foreman.cli.run_live_delivery", side_effect=ValueError(f"Worker error {SYNTHETIC_GITHUB}")),
            redirect_stderr(StringIO()) as stderr,
        ):
            code = main([*base, "--issue", "8"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "Worker error [redacted:GitHub Token]\n")

    def test_only_boundary_functions_reference_redact(self):
        references = Counter()
        package = Path(__file__).resolve().parents[1] / "src" / "foreman"
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text())
            aliases = {"redact"}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in {"foreman.redaction", "redaction"}:
                    aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "redact")

            def visit(node, scope="<module>"):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = node.name if scope == "<module>" else f"{scope}.{node.name}"
                if (
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases
                    or isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr == "redact"
                ):
                    references[path.relative_to(package).as_posix(), scope] += 1
                for child in ast.iter_child_nodes(node):
                    visit(child, scope)

            visit(tree)
        self.assertEqual(references, {
            ("check_output.py", "CheckOutput.__call__"): 1,
            ("dispatch.py", "DispatchRun.clean.scrub"): 1,
            ("cli.py", "_write_run_log"): 1,
            ("smoke_command.py", "run_smoke"): 1,
            ("smoke_command.py", "_run_smoke"): 1,
            ("worker_auth.py", "main"): 1,
            ("worker_auth.py", "environment"): 1,
            ("worker_auth.py", "validate_config"): 1,
            ("github.py", "LiveForge._run_command"): 2,
            ("workspace.py", "LiveWorkspace._git"): 2,
            ("github.py", "LiveForge._arm_auto_merge"): 1,
            ("github.py", "redact_slice"): 7,
            ("github.py", "status_body"): 2,
            ("github.py", "LiveForge.ensure_label"): 1,
            ("github.py", "LiveForge.edit_labels"): 1,
            ("github.py", "LiveForge.add_blocker"): 1,
            ("github.py", "LiveForge.add_sub_issue"): 1,
            ("github.py", "LiveForge.update_issue_body"): 1,
            ("github.py", "LiveForge.create_issue"): 1,
            ("github.py", "LiveForge.comment_issue"): 1,
            ("github.py", "LiveForge.post_review"): 4,
            ("github.py", "pull_request_command"): 5,
            ("steps/implement.py", "prepare"): 1,
            ("steps/implement.py", "commit"): 2,
            ("steps/implement.py", "publish"): 1,
            ("workspace.py", "branch_command"): 1,
            ("workspace.py", "commit_commands"): 1,
            ("workspace.py", "push_command"): 1,
            ("workspace.py", "LiveWorkspace.create_branch"): 1,
            ("workspace.py", "LiveWorkspace.committed"): 1,
            ("workspace.py", "LiveWorkspace.remote_head"): 1,
        })
