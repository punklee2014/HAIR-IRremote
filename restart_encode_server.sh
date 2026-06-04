#!/bin/sh
# Run this on the HA host to restart the AC protocol encode server.
PID_FILE=/tmp/hair_encode_server.pid

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Killed old encode server (PID $PID)"
    fi
    rm -f "$PID_FILE"
fi
# Also kill any leftover from before PID file was added.
pkill -f encode_server.py 2>/dev/null || true

sleep 1
python3 /config/custom_components/hair/encoder/encode_server.py > /tmp/hair_server.log 2>&1 &
echo "New encode server started (PID $!)"
