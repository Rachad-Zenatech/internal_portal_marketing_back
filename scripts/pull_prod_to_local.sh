#!/usr/bin/env bash
# Copy production into the local Docker database (one way: production is only read). See pull_prod_to_local.py.
set -e

for DP in "$LOCALAPPDATA/Programs/DockerDesktop/resources/bin" \
          "/mnt/c/Users/$USER/AppData/Local/Programs/DockerDesktop/resources/bin" \
          "/c/Users/$USER/AppData/Local/Programs/DockerDesktop/resources/bin" \
          "/c/Program Files/Docker/Docker/resources/bin"; do
  if [ -d "$DP" ] && [[ ":$PATH:" != *":$DP:"* ]]; then
    export PATH="$DP:$PATH"
  fi
done

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

PYTHON="venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="venv/Scripts/python.exe"
"$PYTHON" scripts/pull_prod_to_local.py
