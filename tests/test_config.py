import tests

import unittest

from foreman.config import ConfigError, load_config, order_for, parse_config


class ConfigTest(unittest.TestCase):
    def test_packaged_config_loads(self):
        config = load_config()
        self.assertEqual(config.max_review_rounds, 3)
        self.assertEqual(config.nodes["verify"].models, ())
        self.assertEqual(config.nodes["implement"].models[0].name, "gpt-6.1-sol-high")

    def test_only_review_order_changes(self):
        config = load_config()
        coordinator = ["claude-opus-5-5-high", "grok-4.7-high", "gpt-6.1-sol-high"]
        expected = {name: coordinator for name in ("triage", "research", "spec")}
        expected.update(implement=["gpt-6.1-sol-high", "gpt-6.1-terra-high", "claude-opus-5-5-high", "grok-4.7-high"],
                        review=["claude-opus-5-5-high", "gpt-6.1-sol-high"], verify=[])
        self.assertEqual({name: [m.name for m in node.models] for name, node in config.nodes.items()}, expected)

    def test_review_prefers_the_other_family(self):
        config = load_config()
        gpt_first = order_for("review", config, {"implement": "gpt-6.1-sol-high"})
        claude_first = order_for("review", config, {"implement": "claude-opus-5-5-high"})
        self.assertEqual([model.family for model in gpt_first], ["claude", "gpt"])
        self.assertEqual([model.family for model in claude_first], ["gpt", "claude"])

    def test_review_without_a_builder_stops(self):
        config = load_config()
        with self.assertRaises(ConfigError):
            order_for("review", config, {})

    def test_single_model_and_single_family_load_and_select(self):
        for names in (["gpt-6.1-sol-high"], ["gpt-6.1-sol-high", "gpt-6.1-sol-max"]):
            config = parse_config({"max_review_rounds": 3, "nodes": {
                "implement": {"role": "builder", "models": [names[0]]},
                "review": {"role": "verifier", "models": names},
            }})
            self.assertEqual([m.name for m in order_for("review", config, {}, [names[0]])], names)

    def test_stable_partition_uses_all_contributors_and_only_the_configured_list(self):
        names = ["gpt-6.1-sol-high", "claude-opus-5-5-high", "gpt-6.1-sol-max", "claude-opus-5-5-max"]
        config = parse_config({"max_review_rounds": 3, "nodes": {
            "implement": {"role": "builder", "models": ["grok-4.7-high"]},
            "review": {"role": "verifier", "models": names},
            "spec": {"role": "builder", "models": ["claude-opus-5-5-high"]},
            "spec_check": {"role": "verifier", "models": names, "other_family_from": "spec"},
        }})
        for authors, expected in ((["gpt-model"], names[1::2] + names[::2]),
                                  (["claude-model"], names[::2] + names[1::2]),
                                  (["gpt-model", "claude-model"], names)):
            self.assertEqual([m.name for m in order_for("review", config, {}, authors)], expected)
        # A future checker uses its actual author's model, not implementation history.
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
