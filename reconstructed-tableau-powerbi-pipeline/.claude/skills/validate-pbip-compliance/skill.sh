#!/bin/bash
# validate-pbip-compliance skill CLI wrapper
# Runs the Python compliance validation script with passed arguments

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/validate_pbip_compliance.py" "$@"
