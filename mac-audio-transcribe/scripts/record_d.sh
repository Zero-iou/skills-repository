#!/bin/zsh
PID_FILE="$HOME/.hammerspoon/volc_asr_d.pid"

if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill -INT "$PID"
    fi
    exit 0
fi

OUTPUT_DIR="$HOME/workspace/record"
mkdir -p "$OUTPUT_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
TXT_FILE="$OUTPUT_DIR/${TIMESTAMP}-record.txt"

osascript -e 'display notification "再次按 ⌘⇧D 停止并转录" with title "录音中..."'
nohup /usr/bin/python3 "$HOME/.hammerspoon/volc_asr.py" -m d -o "$TXT_FILE" > /dev/null 2>&1 &
