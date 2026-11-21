"""Exercise the configured provider assertions without the unavailable npm runner."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name("run-unit-tests.py")), run_name="offline_policy")
import yaml
from evals.provider import call_api

root = Path(__file__).resolve().parents[1]
# BaseLoader preserves YAML 1.2 strings such as 'yes', as used by Promptfoo.
cases = yaml.load((root / "evals/promptfooconfig.yaml").read_text(), Loader=yaml.BaseLoader)["tests"]
for case in cases:
    variables = case["vars"]
    response = call_api(variables["case"], None, {"vars": variables})["output"]
    for assertion in case["assert"]:
        kind, expected = assertion["type"], assertion["value"]
        if kind == "equals":
            assert response == expected, (case["description"], response, expected)
        elif kind == "contains":
            assert expected in response, (case["description"], response, expected)
        else:
            raise AssertionError(f"Unsupported assertion: {kind}")
    print("PASS", case["description"])
print(f"{len(cases)} provider scenarios passed")
