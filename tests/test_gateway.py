from datetime import timedelta

import jwt
from conftest import token


def test_public_route_is_forwarded(gw):
    r = gw.get("/api/v1/mobile/tuition/20210001")
    assert r.status_code == 200 and r.json()["balance"] == 3000.0
    assert r.headers["X-Request-ID"] and r.headers["X-RateLimit-Remaining"] == "2"


def test_protected_route_requires_jwt_at_the_edge(gw):
    r = gw.get("/api/v1/banking/tuition/20210001")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    h = token(gw, "20210001", "student123")
    assert gw.get("/api/v1/banking/tuition/20210001", headers=h).json()["balance"] == 3000.0


def test_edge_rejects_forged_expired_and_wrong_role_tokens(gw):
    forged = jwt.encode({"sub": "20210001", "role": "student", "iss": "tuition-api"}, "not-the-secret-0123456789abcdef!",
                        algorithm="HS256")
    r = gw.get("/api/v1/banking/tuition/20210001", headers={"Authorization": f"Bearer {forged}"})
    assert r.json()["error"]["code"] == "INVALID_TOKEN"
    student = token(gw, "20210001", "student123")
    r = gw.get("/api/v1/admin/tuition/unpaid", headers=student, params={"term": "2027-SPRING"})
    assert r.status_code == 403 and "admin" in r.json()["error"]["message"]


def test_object_level_authorization_still_enforced_by_service(gw):
    h = token(gw, "20210001", "student123")          # valid student token, someone else's account
    assert gw.get("/api/v1/banking/tuition/20210002", headers=h).status_code == 403


def test_mobile_rate_limit_is_per_student_per_day(gw, clock):
    for remaining in ("2", "1", "0"):
        assert gw.get("/api/v1/mobile/tuition/20210001").headers["X-RateLimit-Remaining"] == remaining
    blocked = gw.get("/api/v1/mobile/tuition/20210001")
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert int(blocked.headers["Retry-After"]) == 14 * 3600          # 10:00 -> midnight Istanbul
    # keyed by student identity, not by client IP: same client, other student is fine
    assert gw.get("/api/v1/mobile/tuition/20210002").status_code == 200
    # different term query for the same student still counts against the same student
    assert gw.get("/api/v1/mobile/tuition/20210001", params={"term": "2027-SPRING"}).status_code == 429
    h = token(gw, "20210001", "student123")
    assert gw.get("/api/v1/banking/tuition/20210001", headers=h).status_code == 200  # limit is mobile-only
    clock.now += timedelta(days=1)
    assert gw.get("/api/v1/mobile/tuition/20210001").status_code == 200   # new day


def test_unknown_version_path_and_method(gw):
    assert gw.get("/api/v2/mobile/tuition/20210001").json()["error"]["code"] == "UNKNOWN_API_VERSION"
    assert gw.get("/api/v1/internal/debug").json()["error"]["code"] == "ROUTE_NOT_FOUND"
    r = gw.delete("/api/v1/mobile/tuition/20210001")
    assert r.status_code == 405 and r.headers["Allow"] == "GET"


def test_route_table_and_swagger_published(gw):
    ids = {r["id"] for r in gw.get("/gateway/routes").json()}
    assert {"mobile-query-tuition", "banking-pay-tuition", "admin-unpaid-tuition"} <= ids
    spec = gw.get("/openapi.json").json()
    assert "/api/v1/banking/payments" in spec["paths"]


def test_api_rejects_direct_calls_when_gateway_secret_is_set(api, gw, monkeypatch):
    """Origin verification used in the cloud: the API accepts /api calls only with the
    secret header that the gateway adds; clients cannot bypass the gateway."""
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", "test-gateway-secret")
    direct = api.get("/api/v1/mobile/tuition/20210002")
    assert direct.status_code == 403 and direct.json()["error"]["code"] == "DIRECT_ACCESS_FORBIDDEN"
    spoofed = api.get("/api/v1/mobile/tuition/20210002", headers={"X-Gateway-Key": "guess"})
    assert spoofed.status_code == 403
    via_gateway = gw.get("/api/v1/mobile/tuition/20210002")
    assert via_gateway.status_code == 200 and via_gateway.json()["balance"] == 9000.0
    assert api.get("/health").status_code == 200                     # health probe stays open
