#!/bin/sh
set -eu
cd "$(dirname "$0")"
PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"
export PATH
mode=start
case "$#:${1:-}:${2:-}" in
    0::) ;;
    1:doctor:) mode=doctor ;;
    2:doctor:--fix) mode=fix ;;
    *) echo 'Usage: Start Evidence OS.command [doctor [--fix]]' >&2; exit 2 ;;
esac
fail() {
    echo "$1" >&2
    if [ "$mode" = start ] && [ -t 0 ]; then printf 'Press Return to close. '; read -r ignored || :; fi
    exit 1
}
[ -f scripts/practice_setup.py ] && [ -f uv.lock ] && [ -f pyproject.toml ] ||
    fail "Setup files are missing. Extract the entire ZIP into a fresh folder."
supports_sync() { "$1" sync --help 2>/dev/null | grep -q -e '--check'; }
uv_bin="$PWD/.evidence-tools/uv"
if ! supports_sync "$uv_bin"; then
    installed=$(command -v uv || :)
    if [ -n "$installed" ] && supports_sync "$installed"; then
        uv_bin="$installed"
    else
        echo "NEEDS ATTENTION  A working copy of the uv setup tool is missing."
        [ "$mode" != doctor ] || fail "Open Doctor Evidence OS to install uv and check Python."
        echo "Setup can download uv 0.11.15 from Astral into this folder."
        printf 'Install it here and continue? [y/N] '
        read -r answer || answer=""
        case "$answer" in
            y|Y|yes|Yes) ;;
            *) fail "Install uv with your IT team, then reopen this file. Guide: https://docs.astral.sh/uv/getting-started/installation/" ;;
        esac
        [ ! -L .evidence-tools ] || fail "The tools directory is linked. Extract the ZIP into a fresh folder."
        installer=$(mktemp) || fail "Choose a writable folder and try again."
        trap 'rm -f "$installer"' EXIT
        curl -fsSL https://astral.sh/uv/0.11.15/install.sh -o "$installer" &&
            UV_UNMANAGED_INSTALL="$PWD/.evidence-tools" sh "$installer" && supports_sync "$uv_bin" ||
            fail "The uv installation failed. Check the network or ask IT to install uv, then retry."
    fi
fi
echo "OK  $("$uv_bin" --version)"
if [ "$mode" = doctor ]; then
    "$uv_bin" python find --system --offline --no-python-downloads 3.12 >/dev/null 2>&1 ||
        fail "NEEDS ATTENTION  Python 3.12 is missing. Open Doctor Evidence OS to install it."
    set -- --offline --no-python-downloads
else
    set --
fi
"$uv_bin" run --no-project --isolated --python 3.12 "$@" -- python -I -B scripts/practice_setup.py "$mode" "$uv_bin" ||
    fail "Setup could not finish. Read the first failed check above. For download errors, check your network and available space."
