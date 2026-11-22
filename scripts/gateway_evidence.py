"""Exercises the running API Gateway and records request/response evidence.

    python -m tuition_api.seed                      # fresh data
    python scripts/gateway_evidence.py [GATEWAY_URL] > docs/evidence/gateway_evidence.md
"""
import json
import sys
import uuid

import httpx

GW = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
c = httpx.Client(base_url=GW, timeout=15)


def show(title, method, path, token=None, **kw):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = c.request(method, path, headers=headers, **kw)
    shown = {k: v for k, v in r.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after", "x-request-id"))}
    print(f"### {title}\n\n```http\n{method} {GW}{path}{'?' + str(httpx.QueryParams(kw['params'])) if kw.get('params') else ''}"
          f"{chr(10) + 'Authorization: Bearer <JWT>' if token else ''}")
    if "json" in kw:
        print(json.dumps(kw["json"]))
    print(f"\nHTTP {r.status_code}" + "".join(f"\n{k}: {v}" for k, v in shown.items()))
    print(json.dumps(r.json(), indent=2)[:1500] + "\n```\n")
    return r


print(f"# API Gateway evidence\n\nGateway URL: `{GW}`\n")
print("## Route mappings (`GET /gateway/routes`)\n\n| id | method | public route | upstream | auth | rate limit |\n|---|---|---|---|---|---|")
for r in c.get("/gateway/routes").json():
    print(f"| {r['id']} | {r['method']} | `{r['publicPath']}` | {r['upstream']} | {r['auth']} | {r['rateLimit'] or '-'} |")
print()

student = c.post("/api/v1/auth/token", json={"username": "20210001", "password": "student123"}).json()["accessToken"]
admin = c.post("/api/v1/auth/token", json={"username": "admin", "password": "admin123"}).json()["accessToken"]

print("## Routed endpoints\n")
show("1. Public route — mobile Query Tuition (no auth)", "GET", "/api/v1/mobile/tuition/20210002")
show("2. Protected route WITHOUT token → rejected at the gateway", "GET", "/api/v1/banking/tuition/20210001")
show("3. Protected route WITH student JWT", "GET", "/api/v1/banking/tuition/20210001", student, params={"term": "2027-SPRING"})
show("4. Student JWT on another student's account → 403 (no access to others' data)", "GET",
     "/api/v1/banking/tuition/20210002", student)
show("5. Student JWT on an admin route → 403 at the gateway", "GET", "/api/v1/admin/tuition/unpaid", student,
     params={"term": "2027-SPRING"})
show("6. Admin: Unpaid Tuition Status (paged)", "GET", "/api/v1/admin/tuition/unpaid", admin,
     params={"term": "2027-SPRING", "page": 1, "pageSize": 3})
txn = "EVID-" + uuid.uuid4().hex[:12].upper()
show("7. Pay Tuition (simulated, partial)", "POST", "/api/v1/banking/payments", student,
     json={"studentNo": "20210001", "term": "2027-SPRING", "amount": 500, "transactionId": txn})
show("8. Same transaction ID again → duplicate rejected", "POST", "/api/v1/banking/payments", student,
     json={"studentNo": "20210001", "term": "2027-SPRING", "amount": 500, "transactionId": txn})
show("9. Amount exceeding balance → rejected", "POST", "/api/v1/banking/payments", student,
     json={"studentNo": "20210001", "term": "2027-SPRING", "amount": 99999, "transactionId": txn + "X"})

print("## Rate limit: 3 mobile queries per student per day (keyed by studentNo)\n")
for i in range(1, 4):
    r = c.get("/api/v1/mobile/tuition/20210003")
    print(f"- call {i}: HTTP {r.status_code}, X-RateLimit-Remaining={r.headers.get('x-ratelimit-remaining')}")
print()
show("4th call for the same student → 429", "GET", "/api/v1/mobile/tuition/20210003")
show("Unknown API version → 404 at the gateway", "GET", "/api/v2/mobile/tuition/20210001")
