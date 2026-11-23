#!/bin/sh
# Model-check Foreman's recovery models with the pinned TLC. Exit 0 only if every model passes.
# Needs Java 11 or later on PATH (or JAVA). Usage: sh formal/check.sh [Model ...]
set -eu

VERSION=1.7.4
SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
here=$(cd "$(dirname "$0")" && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/foreman
jar=$cache/tla2tools-$VERSION.jar
java=${JAVA:-java}

if [ ! -f "$jar" ]; then
    mkdir -p "$cache"
    curl -fsSL -o "$jar.part" "https://github.com/tlaplus/tlaplus/releases/download/v$VERSION/tla2tools.jar"
    mv "$jar.part" "$jar"
fi
echo "$SHA256  $jar" | shasum -a 256 -c - >/dev/null || { echo "tla2tools.jar does not match v$VERSION: $jar" >&2; exit 1; }
"$java" -version 2>&1 | head -n 1

[ "$#" -gt 0 ] || set -- DispatchRecovery PublishReconciliation
meta=$(mktemp -d)
trap 'rm -r "$meta"' EXIT
status=0
for model in "$@"; do
    if (cd "$here" && "$java" -XX:+UseParallelGC -cp "$jar" tlc2.TLC -workers auto -cleanup -metadir "$meta/$model" "$model" >"$meta/$model.log" 2>&1); then
        echo "$model: $(grep -E 'distinct states found' "$meta/$model.log" | tail -n 1)"
    else
        status=1
        echo "$model: FAILED" >&2
        cat "$meta/$model.log" >&2
    fi
done
exit "$status"
