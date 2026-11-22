"""Container entrypoint: one image, the service is chosen with SERVICE.

    SERVICE=tuition-api | gateway | mcp-server | agent      (PORT optional)
"""
import os
import sys

SERVICES = {
    "tuition-api": (["uvicorn", "tuition_api.main:app"], 8001),
    "gateway": (["uvicorn", "gateway.main:app", "--proxy-headers"], 8000),
    "agent": (["uvicorn", "agent.web:app"], 8080),
    "mcp-server": ([sys.executable, "-m", "mcp_server.server", "--transport", "streamable-http"], 8765),
}

name = os.environ.get("SERVICE", "gateway")
if name not in SERVICES:
    sys.exit(f"Unknown SERVICE={name!r}; use one of {', '.join(SERVICES)}")
cmd, default_port = SERVICES[name]
cmd = cmd + ["--host", "0.0.0.0", "--port", os.environ.get("PORT", str(default_port))]
print("starting", name, " ".join(cmd), flush=True)
os.execvp(cmd[0], cmd)
