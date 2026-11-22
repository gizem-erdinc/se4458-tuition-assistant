"""API Gateway — the single public entry point for every REST client
(University Mobile App, Banking App, admin web site and the AI agent).

Policies are declared per route in ``ROUTES`` (the route mapping table):
* versioned public routes ``/api/v1/...`` mapped to the upstream service;
  unknown versions/paths -> 404, wrong method -> 405 (rejected at the edge)
* authentication at the edge: protected routes require a valid JWT (signature,
  expiry, issuer) and an allowed role, otherwise 401/403 before any upstream
  call. The Tuition service re-checks everything (defence in depth) and also
  enforces object-level ownership (a student can only touch its own account).
* rate limiting keyed by STUDENT IDENTITY (the ``studentNo`` path parameter),
  not by client IP: mobile Query Tuition = 3 calls / student / calendar day
  (Europe/Istanbul). Counters live in PostgreSQL, so they are shared by all
  gateway replicas and survive restarts.
* X-Request-ID correlation, JSON access log, 502/504 on upstream failure.

    uvicorn gateway.main:app --port 8000
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(message)s")

TZ = ZoneInfo("Europe/Istanbul")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-secret-change-me-32-bytes-min!")
JWT_ISSUER = "tuition-api"
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
UPSTREAMS = {"tuition-api-v1": os.environ.get("TUITION_API_URL", "http://127.0.0.1:8001")}


@dataclass
class Route:
    id: str
    method: str
    path: str                                   # public path template
    upstream: str = "tuition-api-v1"
    roles: tuple[str, ...] | None = None        # None = public
    daily_limit: int | None = None              # per value of `limit_key_param`
    limit_key_param: str | None = None
    regex: re.Pattern = field(init=False)

    def __post_init__(self):
        self.regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path) + "$")


MOBILE_DAILY_LIMIT = int(os.environ.get("MOBILE_DAILY_LIMIT", "3"))
ROUTES: list[Route] = [
    Route("auth-token", "POST", "/api/v1/auth/token"),
    Route("mobile-query-tuition", "GET", "/api/v1/mobile/tuition/{studentNo}",
          daily_limit=MOBILE_DAILY_LIMIT, limit_key_param="studentNo"),
    Route("banking-query-tuition", "GET", "/api/v1/banking/tuition/{studentNo}", roles=("student", "bank")),
    Route("banking-pay-tuition", "POST", "/api/v1/banking/payments", roles=("student", "bank")),
    Route("admin-add-tuition", "POST", "/api/v1/admin/tuition", roles=("admin",)),
    Route("admin-add-tuition-batch", "POST", "/api/v1/admin/tuition/batch", roles=("admin",)),
    Route("admin-unpaid-tuition", "GET", "/api/v1/admin/tuition/unpaid", roles=("admin",)),
]
SUPPORTED_VERSIONS = {"v1"}


class PostgresDailyRateLimiter:
    """Fixed calendar-day window. One atomic UPSERT per request:
    INSERT … ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count.

    Uses the synchronous psycopg pool in a worker thread: psycopg's async mode
    cannot run on Windows' default ProactorEventLoop, and the query is tiny."""

    def __init__(self, dsn: str, clock: Callable[[], datetime] | None = None):
        self.dsn = dsn
        self.clock = clock or (lambda: datetime.now(TZ))
        self.pool: ConnectionPool | None = None

    def _increment(self, key: str, day) -> int:
        if self.pool is None:
            self.pool = ConnectionPool(self.dsn, min_size=1, max_size=5, open=True,
                                       kwargs={"autocommit": True})
        with self.pool.connection() as conn:
            return conn.execute(
                "INSERT INTO gateway_rate_counters (limit_key, window_day, count) VALUES (%s, %s, 1)"
                " ON CONFLICT (limit_key, window_day) DO UPDATE SET count = gateway_rate_counters.count + 1"
                " RETURNING count", (key, day)).fetchone()[0]

    async def hit(self, key: str, limit: int) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, seconds_until_reset)."""
        now = self.clock()
        count = await run_in_threadpool(self._increment, key, now.date())
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
        return count <= limit, max(limit - count, 0), int((midnight - now).total_seconds())

    async def close(self) -> None:
        if self.pool is not None:
            await run_in_threadpool(self.pool.close)
            self.pool = None


def create_app(transport: httpx.AsyncBaseTransport | None = None,
               limiter: PostgresDailyRateLimiter | None = None) -> FastAPI:
    limiter = limiter or PostgresDailyRateLimiter(
        os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/tuition"))
    clients: dict[str, httpx.AsyncClient] = {}

    def client(name: str) -> httpx.AsyncClient:
        if name not in clients:
            clients[name] = httpx.AsyncClient(base_url=UPSTREAMS[name], transport=transport,
                                              timeout=httpx.Timeout(10.0))
        return clients[name]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for c in clients.values():
            await c.aclose()
        await limiter.close()

    app = FastAPI(title="Tuition API Gateway", version="1.0.0", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.limiter = limiter

    def error(status: int, code: str, message: str, rid: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}},
                            headers={"X-Request-ID": rid, **(headers or {})})

    @app.get("/gateway/health")
    async def health():
        return {"status": "ok", "service": "api-gateway"}

    @app.get("/gateway/routes")
    async def routes():
        return [{"id": r.id, "method": r.method, "publicPath": r.path, "upstream": f"{r.upstream} "
                 f"({UPSTREAMS[r.upstream]}{r.path})", "auth": f"JWT, roles={list(r.roles)}" if r.roles else "none",
                 "rateLimit": f"{r.daily_limit}/day per {r.limit_key_param}" if r.daily_limit else None}
                for r in ROUTES]

    # Swagger UI of the upstream service, published through the gateway.
    @app.get("/docs")
    @app.get("/openapi.json")
    async def docs(request: Request):
        up = await client("tuition-api-v1").get(request.url.path)
        return Response(up.content, up.status_code, media_type=up.headers.get("content-type"))

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(rest: str, request: Request):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        path = request.url.path
        version = rest.split("/", 1)[0]
        if version not in SUPPORTED_VERSIONS:
            return _done(error(404, "UNKNOWN_API_VERSION", f"API version '{version}' is not supported", rid),
                         rid, request, started)
        matches = [(r, m) for r in ROUTES if (m := r.regex.match(path))]
        if not matches:
            return _done(error(404, "ROUTE_NOT_FOUND", f"No public route for {path}", rid), rid, request, started)
        hit = next(((r, m) for r, m in matches if r.method == request.method), None)
        if hit is None:
            return _done(error(405, "METHOD_NOT_ALLOWED", f"{request.method} not allowed on {path}", rid,
                               {"Allow": ", ".join(sorted({r.method for r, _ in matches}))}), rid, request, started)
        route, m = hit

        # ---- authentication / coarse authorization at the edge
        principal = "anonymous"
        if route.roles:
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return _done(error(401, "UNAUTHENTICATED", "Bearer token required", rid,
                                   {"WWW-Authenticate": "Bearer"}), rid, request, started, route)
            try:
                claims = jwt.decode(auth[7:], JWT_SECRET, algorithms=["HS256"], issuer=JWT_ISSUER)
            except jwt.ExpiredSignatureError:
                return _done(error(401, "TOKEN_EXPIRED", "Token expired", rid), rid, request, started, route)
            except jwt.InvalidTokenError:
                return _done(error(401, "INVALID_TOKEN", "Invalid token", rid), rid, request, started, route)
            if claims.get("role") not in route.roles:
                return _done(error(403, "FORBIDDEN", f"Route requires role: {', '.join(route.roles)}", rid),
                             rid, request, started, route)
            principal = f"{claims['role']}:{claims['sub']}"

        # ---- rate limit keyed by student identity
        extra: dict[str, str] = {}
        if route.daily_limit:
            key = f"{route.id}:{m.group(route.limit_key_param)}"
            allowed, remaining, reset = await limiter.hit(key, route.daily_limit)
            extra = {"X-RateLimit-Limit": str(route.daily_limit), "X-RateLimit-Remaining": str(remaining),
                     "X-RateLimit-Reset": str(reset)}
            if not allowed:
                return _done(error(429, "RATE_LIMIT_EXCEEDED",
                                   f"Query Tuition is limited to {route.daily_limit} calls per student per day",
                                   rid, {**extra, "Retry-After": str(reset)}), rid, request, started, route)

        # ---- forward
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() != "x-gateway-key"}
        if os.environ.get("GATEWAY_SHARED_SECRET"):          # origin verification (see tuition_api)
            headers["x-gateway-key"] = os.environ["GATEWAY_SHARED_SECRET"]
        headers.update({"x-request-id": rid, "x-gateway-route": route.id,
                        "x-forwarded-for": request.client.host if request.client else "unknown"})
        try:
            up = await client(route.upstream).request(request.method, path, params=request.query_params,
                                                      headers=headers, content=await request.body())
        except httpx.TimeoutException:
            return _done(error(504, "UPSTREAM_TIMEOUT", "Tuition service did not respond in time", rid),
                         rid, request, started, route)
        except httpx.TransportError:
            return _done(error(502, "UPSTREAM_UNAVAILABLE", "Tuition service is unavailable", rid),
                         rid, request, started, route)
        out = {k: v for k, v in up.headers.items() if k.lower() not in HOP_BY_HOP}
        out.update(extra)
        out["X-Request-ID"] = rid
        return _done(Response(up.content, up.status_code, headers=out), rid, request, started, route, principal)

    return app


def _done(resp: Response, rid: str, request: Request, started: float, route: Route | None = None,
          principal: str = "-") -> Response:
    log.info(json.dumps({"ts": datetime.now(TZ).isoformat(timespec="seconds"), "rid": rid,
                         "route": route.id if route else None, "method": request.method,
                         "path": request.url.path, "status": resp.status_code, "principal": principal,
                         "ms": round((time.perf_counter() - started) * 1000, 1)}))
    return resp


app = create_app()
