#!/bin/sh
# Promptfoo's worker does not use the uv environment unless told which interpreter to start.
set -eu
cd "$(dirname "$0")/.."
if [ -z "${PROMPTFOO_PYTHON:-}" ]; then
  if [ -x .venv/bin/python ]; then
    PROMPTFOO_PYTHON="$PWD/.venv/bin/python"
  else
    PROMPTFOO_PYTHON="$(command -v python)"
  fi
  export PROMPTFOO_PYTHON
fi
exec npx --yes promptfoo@0.125.1 eval -c evals/promptfooconfig.yaml "$@"
