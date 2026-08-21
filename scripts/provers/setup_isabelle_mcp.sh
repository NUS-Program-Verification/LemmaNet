#!/usr/bin/env bash
# Install Isabelle-MCP, which the `isabelle` prover backend drives.
#
# The version is pinned in scripts/provers/PINS.toml. Run this inside the
# development container, with Isabelle2025-2 on PATH.
#
# Unlike IsaREPL, this needs no patched Isabelle: the package ships a prebuilt
# `isabelle mcp_server` Scala component that it registers on first use, so no
# session heap is invalidated and nothing is compiled here.
set -euo pipefail

VERSION="isabelle-mcp==0.3.1"

usage() {
    cat <<EOF
Usage: $0 [--version SPEC] [--check]

  --version SPEC  pip requirement to install (default: $VERSION)
  --check         verify an existing installation without installing

Registers the Isabelle component and reports the Isabelle it bound to.
Undo the registration with: isabelle-mcp uninstall
EOF
}

CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) VERSION="$2"; shift 2 ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v isabelle >/dev/null || { echo "isabelle is not on PATH" >&2; exit 1; }

ISABELLE_VERSION="$(isabelle version)"
if [[ "$ISABELLE_VERSION" != "Isabelle2025-2" ]]; then
    echo "warning: Isabelle-MCP supports Isabelle2025-2; found $ISABELLE_VERSION" >&2
fi

if [[ "$CHECK_ONLY" == 0 ]]; then
    pip install --quiet "$VERSION"
fi

python3 - <<'PY'
from isabelle_mcp import component
c = component.ensure_component()
print(f"Isabelle-MCP component registered for {c.identifier}")
print(f"  isabelle: {c.isabelle}")
print(f"  component: {c.path}")
PY
