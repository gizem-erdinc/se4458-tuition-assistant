"""Local launcher without Docker for the Python services (Windows / macOS / Linux).
PostgreSQL must already be running (e.g. `docker compose up -d db`, or a local
install) and reachable through DATABASE_URL.

    python run_all.py            # seed DB, build index, start all services
    python run_all.py --no-seed  # keep existing data

Services
    API Gateway        http://127.0.0.1:8000   (Swagger UI: /docs, route table: /gateway/routes)
    Tuition REST API   http://127.0.0.1:8001   (internal; clients use the gateway)
    MCP policy server  http://127.0.0.1:8765/mcp  (Streamable HTTP)
    AI Agent chat UI   http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def wait(url: str, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise SystemExit(f"Service did not come up: {url}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-seed", action="store_true")
    args = ap.parse_args()
    load_dotenv()
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    env.setdefault("MCP_SERVER_URL", "http://127.0.0.1:8765/mcp")
    py = sys.executable
    try:
        import psycopg
        psycopg.connect(env.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/tuition"),
                        connect_timeout=3).close()
    except Exception as e:
        raise SystemExit(f"PostgreSQL is not reachable ({e}).\nStart it with: docker compose up -d db")
    if not args.no_seed:
        subprocess.run([py, "-m", "tuition_api.seed"], cwd=ROOT, env=env, check=True)
    subprocess.run([py, "-m", "mcp_server.indexer"], cwd=ROOT, env=env, check=True)

    procs = []
    try:
        procs.append(subprocess.Popen([py, "-m", "uvicorn", "tuition_api.main:app", "--port", "8001"], cwd=ROOT, env=env))
        wait("http://127.0.0.1:8001/health")
        procs.append(subprocess.Popen([py, "-m", "uvicorn", "gateway.main:app", "--port", "8000"], cwd=ROOT, env=env))
        wait("http://127.0.0.1:8000/gateway/health")
        procs.append(subprocess.Popen([py, "-m", "mcp_server.server", "--transport", "streamable-http",
                                       "--port", "8765"], cwd=ROOT, env=env))
        time.sleep(2)
        procs.append(subprocess.Popen([py, "-m", "uvicorn", "agent.web:app", "--port", "8080"], cwd=ROOT, env=env))
        wait("http://127.0.0.1:8080/", timeout=40)
        print("\nAll services up.  Chat UI: http://127.0.0.1:8080   Gateway: http://127.0.0.1:8000"
              "   Swagger: http://127.0.0.1:8000/docs   MCP: http://127.0.0.1:8765/mcp\nCtrl+C to stop.\n")
        while all(p.poll() is None for p in procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)


if __name__ == "__main__":
    main()
