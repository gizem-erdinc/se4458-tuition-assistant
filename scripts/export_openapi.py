"""Writes the OpenAPI (Swagger) specification to docs/openapi.json.

    python scripts/export_openapi.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tuition_api.main import app  # noqa: E402

spec = app.openapi()
spec["servers"] = [{"url": "http://localhost:8000", "description": "API Gateway (local)"},
                   {"url": "https://<your-gateway-host>", "description": "API Gateway (cloud)"}]
(ROOT / "docs" / "openapi.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
print(f"docs/openapi.json: {len(spec['paths'])} paths")
