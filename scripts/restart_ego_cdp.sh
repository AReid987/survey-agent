#!/bin/bash
# Restart ego-lite with Chrome DevTools Protocol enabled
# This allows Playwright to connect and use CDP mouse events for drag-and-drop

echo "Quitting ego-lite..."
osascript -e 'quit app "ego lite"' 2>/dev/null
sleep 3

echo "Starting ego-lite with CDP port 9222..."
open -a "ego lite" --args --remote-debugging-port=9222
sleep 5

echo "Verifying CDP..."
curl -s http://localhost:9222/json/version | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Browser: {d[\"Browser\"]}')
print(f'CDP Protocol: {d[\"Protocol-Version\"]}')
"

echo "Done. ego-lite running with CDP on port 9222."
