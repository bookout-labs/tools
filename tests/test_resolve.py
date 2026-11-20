import json
import unittest

from foreman.config import ConfigError
from foreman.resolve import Cmd, cache_resolve, newest_codex, newest_grok, resolve_route


CATALOG = {
    "models": [
        {"slug": "gpt-6-sol", "supported_reasoning_levels": [{"effort": "high"}]},
        {"slug": "gpt-6.1-sol", "supported_reasoning_levels": [{"effort": "high"}]},
        {"slug": "gpt-6.2-sol", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "xhigh"}]},
        {"slug": "gpt-6.1-terra", "supported_reasoning_levels": [{"effort": "high"}]},
        {"slug": "gpt-6.2-astra", "supported_reasoning_levels": [{"effort": "high"}]},
    ]
}
GROK = "Available models:\n  * grok-4.7 (default)\n  - grok-4.7-build-fast\n  - grok-4.6\n"


class ResolveTest(unittest.TestCase):
    def test_newest_family_member_at_the_requested_effort(self):
        self.assertEqual(newest_codex(CATALOG, "sol", "high"), "gpt-6.2-sol-high")
        self.assertEqual(newest_codex(CATALOG, "terra", "high"), "gpt-6.1-terra-high")
        self.assertEqual(newest_grok(GROK), "grok-4.7")

    def test_missing_cli_is_an_error(self):
        def run(args):
            raise FileNotFoundError(args[0])

        with self.assertRaises(ConfigError):
            resolve_route("sol", "high", run)

    def test_resolver_uses_the_cli_catalog_and_caches_it(self):
        calls = []

        def run(args):
            calls.append(args)
            if args[:3] == ["codex", "debug", "models"]:
                return Cmd(0, json.dumps(CATALOG))
            if args == ["grok", "models"]:
                return Cmd(0, GROK)
            if args == ["claude", "--version"]:
                return Cmd(0, "claude 1.0\n")
            return Cmd(1, "", "unexpected")

        cached = cache_resolve(lambda route, effort: resolve_route(route, effort, run))
        self.assertEqual(cached("sol", "high"), "gpt-6.2-sol-high")
        self.assertEqual(cached("sol", "high"), "gpt-6.2-sol-high")
        self.assertEqual(cached("terra", "high"), "gpt-6.1-terra-high")
        self.assertEqual(cached("grok", "high"), "grok-4.7")
        self.assertEqual(cached("opus", "high"), "opus")
        self.assertEqual(calls.count(["codex", "debug", "models"]), 2)
        self.assertEqual(calls.count(["claude", "--version"]), 1)


if __name__ == "__main__":
    unittest.main()
