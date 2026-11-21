"""Run the offline suite when the execution sandbox forbids even loopback bind."""
from pathlib import Path
import sys
import types
import unittest

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tests"), str(root / "src")]
policy = root / "tests" / "__init__.py"
source = policy.read_text()
start = source.index('with socket.socket() as proxy:')
end = source.index('os.environ["GIT_ALLOW_PROTOCOL"]', start)
# Preserve the suite's network audit policy; only avoid allocating its closed proxy port.
source = source[:start] + '_proxy_url = "http://127.0.0.1:9"\n' + source[end:]
module = types.ModuleType("tests")
module.__file__ = str(policy)
module.__path__ = [str(root / "tests")]
sys.modules["tests"] = module
exec(compile(source, str(policy), "exec"), module.__dict__)
if __name__ == "__main__":
    unittest.main(module=None, argv=[sys.argv[0], *sys.argv[1:]])
