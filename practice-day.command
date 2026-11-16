#!/bin/bash
# Practise on a past day: rebuilds it from 1-minute candles (if not saved yet) and replays it on port 8766,
# so the live app on 8765 keeps running. Usage: bash practice-day.command [YYYY-MM-DD] [--with-stocks]
cd "$(dirname "$0")" || exit 1
DAY="$1"
if [ -z "$DAY" ]; then
  read -r -p "Which day to practise? (YYYY-MM-DD, e.g. 2026-11-12): " DAY
fi
STOCKS="--indices-only"; [ "$2" = "--with-stocks" ] && STOCKS=""
REC="data/sessions/$DAY"; REB="data/sessions/$DAY-rebuilt"
if [ -d "$REC" ] && [ -n "$(ls "$REC" 2>/dev/null)" ]; then
  FOLDER="$REC"; echo "Using the live recording of $DAY."
else
  echo "Rebuilding $DAY from Upstox 1-minute candles..."
  .venv/bin/python -m app rebuild "$DAY" $STOCKS || { read -n 1 -s -r -p "Rebuild failed. Press any key"; exit 1; }
  FOLDER="$REB"
fi
echo "Replaying $DAY at http://127.0.0.1:8766  (Pause / speed / Restart in the top bar; Ctrl+C to stop)"
exec .venv/bin/python -m app --replay "$FOLDER" --port 8766 --speed 30
