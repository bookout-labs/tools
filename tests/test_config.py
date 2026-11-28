import tests

import unittest
from unittest.mock import patch

from foreman.config import DEFAULT_PATH, ConfigError, load_config, order_for, parse_config


def policy():
    return parse_config({"max_review_rounds": 2, "nodes": {
        "implement": {"role": "builder", "models": ["gpt-builder"]},
        "review": {"role": "reviewer", "models": ["claude-reviewer", "gpt-reviewer"]},
        "verify": {"role": "verifier", "models": ["claude-verifier", "gpt-verifier"], "other_family_from": "implement"},
    }})


class ConfigTest(unittest.TestCase):
    def test_environment_order_overrides_each_workers_models_only(self):
        packaged = load_config(DEFAULT_PATH)
        with patch.dict("os.environ", {"FOREMAN_MODELS": "gpt-first-high, claude-next, grok-last"}):
            selected = load_config()
            self.assertEqual(load_config(DEFAULT_PATH), packaged)
        self.assertEqual(selected.max_review_rounds, packaged.max_review_rounds)
        self.assertEqual(selected.pstack, packaged.pstack)
        self.assertEqual({name: node.role for name, node in selected.nodes.items()},
                         {name: node.role for name, node in packaged.nodes.items()})
        for name, node in selected.nodes.items():
            with self.subTest(node=name):
                self.assertEqual([model.name for model in node.models], ["gpt-first-high", "claude-next", "grok-last"])
                self.assertEqual(node.models[0].effort, "high")

    def test_invalid_environment_order_is_rejected(self):
        for order in ("", "gpt-first,", "unknown-model"):
            with self.subTest(order=order), patch.dict("os.environ", {"FOREMAN_MODELS": order}):
                with self.assertRaises(ConfigError):
                    load_config()

    def test_review_prefers_the_other_family(self):
        config = policy()
        for builder, expected in (("gpt-builder", ["claude", "gpt"]), ("claude-builder", ["gpt", "claude"])):
            selected = order_for("review", config, {"implement": builder})
            self.assertEqual([model.family for model in selected], expected)

    def test_review_without_a_builder_uses_configured_order(self):
        self.assertEqual([model.name for model in order_for("review", policy(), {})], ["claude-reviewer", "gpt-reviewer"])

    def test_verify_counts_every_builder_like_review(self):
        config = policy()
        # The latest builder alone would put gpt last; the earlier Claude builder makes both families contributors.
        for builders, families in ((["claude-builder"], ["gpt", "claude"]), (["gpt-builder"], ["claude", "gpt"]),
                                   (["claude-builder", "gpt-builder"], ["claude", "gpt"])):
            for node in ("review", "verify"):
                with self.subTest(node=node, builders=builders):
                    models = order_for(node, config, {"implement": builders[-1]}, builders)
                    self.assertEqual([model.family for model in models], families)

    def test_single_model_and_single_family_load_and_select(self):
        for names in (["gpt-6.1-sol-high"], ["gpt-6.1-sol-high", "gpt-6.1-sol-max"]):
            config = parse_config({"max_review_rounds": 3, "nodes": {
                "implement": {"role": "builder", "models": [names[0]]},
                "review": {"role": "reviewer", "models": names},
            }})
            self.assertEqual([m.name for m in order_for("review", config, {}, [names[0]])], names)

    def test_stable_partition_uses_all_contributors_and_only_the_configured_list(self):
        names = ["gpt-6.1-sol-high", "claude-opus-5-5-high", "gpt-6.1-sol-max", "claude-opus-5-5-max"]
        config = parse_config({"max_review_rounds": 3, "nodes": {
            "implement": {"role": "builder", "models": ["grok-4.7-high"]},
            "review": {"role": "reviewer", "models": names},
            "spec": {"role": "builder", "models": ["claude-opus-5-5-high"]},
            "spec_check": {"role": "reviewer", "models": names, "other_family_from": "spec"},
        }})
        for authors, expected in ((["gpt-model"], names[1::2] + names[::2]),
                                  (["claude-model"], names[::2] + names[1::2]),
                                  (["gpt-model", "claude-model"], names)):
            self.assertEqual([m.name for m in order_for("review", config, {}, authors)], expected)
        selected = order_for("spec_check", config, {"spec": "claude-model", "implement": "gpt-model"}, ["gpt-model"])
        self.assertEqual([m.name for m in selected], names[::2] + names[1::2])

    def test_unhashable_role_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "implement has an unknown role"):
            parse_config({
                "max_review_rounds": 3,
                "nodes": {"implement": {"role": [], "models": []}},
            })

    def test_unknown_family_is_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config(
                {
                    "max_review_rounds": 3,
                    "nodes": {
                        "implement": {"role": "builder", "models": ["llama-3"]},
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()
