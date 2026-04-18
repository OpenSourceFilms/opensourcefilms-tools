#!/usr/bin/env bash
# Create the persistent Python venv and install all CPU deps.
# Run once; safe to re-run (uv will skip already-installed packages).

set -o pipefail
VENV=/workspace/.pod-persist/venvs/pipeline-v2
UV=/workspace/.pod-persist/bin/uv

echo "=== Installing Python dependencies ==="
echo "  Venv: $VENV"

if [ ! -d "$VENV" ]; then
    "$UV" venv "$VENV" --python 3.11
    echo "  Created venv"
fi

"$UV" pip install --python "$VENV/bin/python" \
    -r /workspace/pipeline/requirements.txt

echo "=== Python deps installed ==="
