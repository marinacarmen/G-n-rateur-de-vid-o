#!/bin/sh
# APP_MODE=ui  -> interface web Streamlit
# sinon        -> serveur MCP pour Claude (défaut)
if [ "$APP_MODE" = "ui" ]; then
  exec streamlit run app.py --server.port "${PORT:-8501}" --server.address 0.0.0.0 --server.headless true
else
  exec python mcp_server.py
fi
