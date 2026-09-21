#!/usr/bin/env bash
# Double-clickable launcher for macOS Finder: opens Terminal and runs start.sh
# from the repo folder, whatever the shell's working directory happens to be.
#
# macOS marks a downloaded file non-executable. If double-clicking does nothing,
# run this once in Terminal:   chmod +x start.command start.sh
cd "$(dirname "$0")"
exec ./start.sh "$@"
