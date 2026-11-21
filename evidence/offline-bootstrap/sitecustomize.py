"""Apply the socket-free offline test bootstrap in child Python processes."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "run-unit-tests.py"), run_name="offline_policy")
