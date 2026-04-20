#!/bin/sh
# Entrypoint: runs the snapapp Flask server on port 8080.
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):$PYTHONPATH"
exec python3 -m app.server --port 8080 --host 0.0.0.0 "$@"
