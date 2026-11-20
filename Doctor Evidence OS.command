#!/bin/sh
cd "$(dirname "$0")" || exit 1
sh "./Start Evidence OS.command" doctor --fix
result=$?
if [ -t 0 ]; then printf 'Press Return to close. '; read -r ignored || :; fi
exit "$result"
