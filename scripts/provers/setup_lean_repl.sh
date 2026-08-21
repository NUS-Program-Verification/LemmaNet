#!/usr/bin/env bash
# Build the Lean 4 REPL that the `lean` prover backend drives.
#
# The commit is pinned in scripts/provers/PINS.toml and matches the Lean
# toolchain NTP4VC's obligations are generated against. Run this inside the
# development container.
set -euo pipefail

REPOSITORY="https://github.com/leanprover-community/repl"
COMMIT="b4967b310e5f776a73e992f936224f9e4028f0d7"
PREFIX="${LEMMANET_PROVER_PREFIX:-$HOME/.lemmanet/provers}"
TARGET="$PREFIX/repl"

usage() {
    cat <<EOF
Usage: $0 [--prefix DIR] [--force]

  --prefix DIR   Install under DIR (default: \$LEMMANET_PROVER_PREFIX or
                 \$HOME/.lemmanet/provers)
  --force        Remove an existing checkout before cloning

Prints the resulting executable path. Export it as LEAN_REPL_PATH, or set
lean.repl_path in the agent configuration.
EOF
}

FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; TARGET="$PREFIX/repl"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v lake >/dev/null || { echo "lake is not on PATH; install Lean via elan" >&2; exit 1; }

if [[ "$FORCE" == 1 ]]; then
    rm -rf "$TARGET"
fi

mkdir -p "$PREFIX"
if [[ ! -d "$TARGET/.git" ]]; then
    git clone --quiet "$REPOSITORY" "$TARGET"
fi

git -C "$TARGET" fetch --quiet origin "$COMMIT" 2>/dev/null || git -C "$TARGET" fetch --quiet origin
git -C "$TARGET" checkout --quiet "$COMMIT"

# The REPL must be built with the same toolchain as the proof obligations.
echo "Building the Lean REPL at $(cat "$TARGET/lean-toolchain")" >&2
(cd "$TARGET" && lake build >&2)

EXECUTABLE="$TARGET/.lake/build/bin/repl"
[[ -x "$EXECUTABLE" ]] || { echo "build finished without producing $EXECUTABLE" >&2; exit 1; }

echo "$EXECUTABLE"
