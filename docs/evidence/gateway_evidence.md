# API Gateway evidence

Gateway URL: `http://127.0.0.1:8000`

## Route mappings (`GET /gateway/routes`)

| id | method | public route | upstream | auth | rate limit |
|---|---|---|---|---|---|
| auth-token | POST | `/api/v1/auth/token` | tuition-api-v1 (http://127.0.0.1:8001/api/v1/auth/token) | none | - |
| mobile-query-tuition | GET | `/api/v1/mobile/tuition/{studentNo}` | tuition-api-v1 (http://127.0.0.1:8001/api/v1/mobile/tuition/{studentNo}) | none | 3/day per studentNo |
| banking-query-tuition | GET | `/api/v1/banking/tuition/{studentNo}` | tuition-api-v1 (http://127.0.0.1:8001/api/v1/banking/tuition/{studentNo}) | JWT, roles=['student', 'bank'] | - |
| banking-pay-tuition | POST | `/api/v1/banking/payments` | tuition-api-v1 (http://127.0.0.1:8001/api/v1/banking/payments) | JWT, roles=['student', 'bank'] | - |
| admin-add-tuition | POST | `/api/v1/admin/tuition` | tuition-api-v1 (http://127.0.0.1:8001/api/v1/admin/tuition) | JWT, roles=['admin'] | - |
| admin-add-tuition-batch | POST | `/api/v1/admin/tuition/batch` | tuition-api-v1 (http://127.0.0.1:8001/api/v1/admin/tuition/batch) | JWT, roles=['admin'] | - |
| admin-unpaid-tuition | GET | `/api/v1/admin/tuition/unpaid` | tuition-api-v1 (http://127.0.0.1:8001/api/v1/admin/tuition/unpaid) | JWT, roles=['admin'] | - |

## Routed endpoints

### 1. Public route ÔÇö mobile Query Tuition (no auth)

```http
GET http://127.0.0.1:8000/api/v1/mobile/tuition/20210002

HTTP 200
x-ratelimit-limit: 3
x-ratelimit-remaining: 2
x-ratelimit-reset: 26476
x-request-id: 8cc279e5271f4da1
{
  "studentNo": "20210002",
  "scope": "OVERALL",
  "term": null,
  "currency": "USD",
  "tuitionTotal": 9000.0,
  "paidTotal": 0.0,
  "balance": 9000.0,
  "terms": [
    {
      "term": "2026-FALL",
      "tuitionTotal": 4500.0,
      "paidTotal": 0.0,
      "balance": 4500.0,
      "status": "UNPAID"
    },
    {
      "term": "2027-SPRING",
      "tuitionTotal": 4500.0,
      "paidTotal": 0.0,
      "balance": 4500.0,
      "status": "UNPAID"
    }
  ]
}
```

### 2. Protected route WITHOUT token ÔåÆ rejected at the gateway

```http
GET http://127.0.0.1:8000/api/v1/banking/tuition/20210001

HTTP 401
x-request-id: 66b78c8492504f95
{
  "error": {
    "code": "UNAUTHENTICATED",
    "message": "Bearer token required"
  }
}
```

### 3. Protected route WITH student JWT

```http
GET http://127.0.0.1:8000/api/v1/banking/tuition/20210001?term=2027-SPRING
Authorization: Bearer <JWT>

HTTP 200
x-request-id: f841d9485ce0489e
{
  "studentNo": "20210001",
  "scope": "TERM",
  "term": "2027-SPRING",
  "currency": "USD",
  "tuitionTotal": 6000.0,
  "paidTotal": 3000.0,
  "balance": 3000.0,
  "terms": [
    {
      "term": "2027-SPRING",
      "tuitionTotal": 6000.0,
      "paidTotal": 3000.0,
      "balance": 3000.0,
      "status": "PARTIALLY_PAID"
    }
  ]
}
```

### 4. Student JWT on another student's account ÔåÆ 403 (no access to others' data)

```http
GET http://127.0.0.1:8000/api/v1/banking/tuition/20210002
Authorization: Bearer <JWT>

HTTP 403
x-request-id: 8a125c32c8b94edf
{
  "error": {
    "code": "FORBIDDEN",
    "message": "Not allowed to access another student's account"
  }
}
```

### 5. Student JWT on an admin route ÔåÆ 403 at the gateway

```http
GET http://127.0.0.1:8000/api/v1/admin/tuition/unpaid?term=2027-SPRING
Authorization: Bearer <JWT>

HTTP 403
x-request-id: 47dc5b17684642ee
{
  "error": {
    "code": "FORBIDDEN",
    "message": "Route requires role: admin"
  }
}
```

### 6. Admin: Unpaid Tuition Status (paged)

```http
GET http://127.0.0.1:8000/api/v1/admin/tuition/unpaid?term=2027-SPRING&page=1&pageSize=3
Authorization: Bearer <JWT>

HTTP 200
x-request-id: 87840e53637b46c4
{
  "term": "2027-SPRING",
  "page": 1,
  "pageSize": 3,
  "totalItems": 23,
  "totalPages": 8,
  "items": [
    {
      "studentNo": "20210001",
      "tuitionTotal": 6000.0,
      "paidTotal": 3000.0,
      "balance": 3000.0,
      "currency": "USD"
    },
    {
      "studentNo": "20210002",
      "tuitionTotal": 4500.0,
      "paidTotal": 0.0,
      "balance": 4500.0,
      "currency": "USD"
    },
    {
      "studentNo": "20210003",
      "tuitionTotal": 200.0,
      "paidTotal": 0.0,
      "balance": 200.0,
      "currency": "USD"
    }
  ]
}
```

### 7. Pay Tuition (simulated, partial)

```http
POST http://127.0.0.1:8000/api/v1/banking/payments
Authorization: Bearer <JWT>
{"studentNo": "20210001", "term": "2027-SPRING", "amount": 500, "transactionId": "EVID-217BDA9C7246"}

HTTP 201
x-request-id: b82dc8bea0fd44ec
{
  "paymentStatus": "SUCCESS",
  "coverage": "PARTIAL",
  "transactionId": "EVID-217BDA9C7246",
  "studentNo": "20210001",
  "term": "2027-SPRING",
  "paidAmount": 500.0,
  "remainingBalance": 2500.0,
  "currency": "USD",
  "paidAt": "2026-11-22T13:38:43+00:00"
}
```

### 8. Same transaction ID again ÔåÆ duplicate rejected

```http
POST http://127.0.0.1:8000/api/v1/banking/payments
Authorization: Bearer <JWT>
{"studentNo": "20210001", "term": "2027-SPRING", "amount": 500, "transactionId": "EVID-217BDA9C7246"}

HTTP 409
x-request-id: a822e898606b4087
{
  "error": {
    "code": "DUPLICATE_TRANSACTION",
    "message": "Transaction EVID-217BDA9C7246 was already processed; it was not recorded again",
    "details": {
      "originalTransaction": {
        "studentNo": "20210001",
        "term": "2027-SPRING",
        "amount": 500.0,
        "recordedAt": "2026-11-22T13:38:43+00:00"
      }
    }
  }
}
```

### 9. Amount exceeding balance ÔåÆ rejected

```http
POST http://127.0.0.1:8000/api/v1/banking/payments
Authorization: Bearer <JWT>
{"studentNo": "20210001", "term": "2027-SPRING", "amount": 99999, "transactionId": "EVID-217BDA9C7246X"}

HTTP 422
x-request-id: 8aaf194904434966
{
  "error": {
    "code": "AMOUNT_EXCEEDS_BALANCE",
    "message": "Amount 99999.0 exceeds outstanding balance 2500.0",
    "details": {
      "outstandingBalance": 2500.0,
      "currency": "USD"
    }
  }
}
```

## Rate limit: 3 mobile queries per student per day (keyed by studentNo)

- call 1: HTTP 200, X-RateLimit-Remaining=2
- call 2: HTTP 200, X-RateLimit-Remaining=1
- call 3: HTTP 200, X-RateLimit-Remaining=0

### 4th call for the same student ÔåÆ 429

```http
GET http://127.0.0.1:8000/api/v1/mobile/tuition/20210003

HTTP 429
x-request-id: c5ece7f7539440c5
x-ratelimit-limit: 3
x-ratelimit-remaining: 0
x-ratelimit-reset: 26476
retry-after: 26476
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Query Tuition is limited to 3 calls per student per day"
  }
}
```

### Unknown API version ÔåÆ 404 at the gateway

```http
GET http://127.0.0.1:8000/api/v2/mobile/tuition/20210001

HTTP 404
x-request-id: 5cbd4b30fd074694
{
  "error": {
    "code": "UNKNOWN_API_VERSION",
    "message": "API version 'v2' is not supported"
  }
}
```

