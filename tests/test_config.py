import unittest

from foreman.config import DEFAULT_PATH, ConfigError, load_config, order_for, parse_config


class ConfigTest(unittest.TestCase):
    def test_packaged_config_loads(self):
        config = load_config()
        self.assertEqual(config.max_review_rounds, 3)
        self.assertEqual(config.nodes["verify"].models, ())
        self.assertEqual(config.effort, "high")
        self.assertEqual(config.nodes["implement"].models, ("sol", "terra", "opus", "grok"))
        text = DEFAULT_PATH.read_text()
        for banned in ("gpt-", "claude-", "grok-"):
            self.assertNotIn(banned, text)

    def test_review_starts_with_a_fresh_sol_session(self):
        config = load_config()
        for builder in ("sol", "opus"):
            order = order_for("review", config, {"implement": builder})
            self.assertEqual(order[0], "sol")
            self.assertNotIn("opus", order)

    def test_review_without_a_builder_stops(self):
        config = load_config()
        with self.assertRaises(ConfigError):
            order_for("review", config, {})

    def test_review_refuses_the_builder_family(self):
        config = parse_config(
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
        with self.assertRaises(ConfigError):
            order_for("review", config, {"implement": "gpt-6.1-sol-high"})

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
