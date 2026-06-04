#!/bin/sh
set -e

MARKER="/home/node/.n8n/.wetsoda-initialized"

if [ ! -f "$MARKER" ]; then
  echo "[wetsoda] First run — importing credentials and workflows..."

  if ls /import/credentials/*.json 1>/dev/null 2>&1; then
    n8n import:credentials --input=/import/credentials/ \
      && echo "[wetsoda] Credentials imported." \
      || echo "[wetsoda] Warning: credential import failed — configure manually in UI."
  fi

  if ls /import/workflows/*.json 1>/dev/null 2>&1; then
    n8n import:workflow --input=/import/workflows/ \
      && echo "[wetsoda] Workflows imported." \
      || echo "[wetsoda] Warning: workflow import failed — import manually in UI."
  fi

  touch "$MARKER"
  echo "[wetsoda] Init complete."
fi

exec n8n "$@"
