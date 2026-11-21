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

    def test_review_uses_the_other_family(self):
        config = load_config()
        gpt_first = order_for("review", config, {"implement": "gpt-6.1-sol-high"})
        claude_first = order_for("review", config, {"implement": "claude-opus-5-5-high"})
        self.assertEqual([model.family for model in gpt_first], ["claude"])
        self.assertEqual([model.family for model in claude_first], ["gpt"])

    def test_review_without_a_builder_stops(self):
        config = load_config()
        with self.assertRaises(ConfigError):
            order_for("review", config, {})

    def test_review_refuses_the_builder_family(self):
        with self.assertRaisesRegex(ConfigError, "no review model outside the gpt family"):
            parse_config(
                {
                    "max_review_rounds": 3,
                    "nodes": {
                        "implement": {"role": "builder", "models": ["gpt-6.1-sol-high"]},
                        "review": {
                            "role": "verifier",
                            "other_family_from": "implement",
                            "models": ["gpt-6.1-terra-high"],
                        },
                    },
                }
            )

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
