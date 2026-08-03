#!/usr/bin/bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$project_dir${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m minirec "$@"
