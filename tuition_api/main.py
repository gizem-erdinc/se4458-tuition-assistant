"""Tuition REST API (business service). Reached only through the API Gateway.

    uvicorn tuition_api.main:app --port 8001
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from tuition_api import db, service
from tuition_api.schemas import (TERM_PATTERN, STUDENT_NO_PATTERN, AddTuitionRequest, AddTuitionResponse,
                                 BatchResponse, PaymentRequest, PaymentResponse, TokenRequest, TokenResponse,
                                 TuitionQueryResponse, UnpaidPage)
from tuition_api.security import (Principal, ensure_can_access_student, issue_token, require_roles,
                                  verify_password)
from tuition_api.service import ApiError

MAX_CSV_BYTES = 1_000_000


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield
    db.close_pool()


app = FastAPI(
    title="Tuition API", version="1.0.0", lifespan=lifespan,
    description=(
        "Tuition business service for the University Mobile App, Banking App, University Web Site (admin) "
        "and the AI Tuition Assistant. **All public traffic goes through the API Gateway** "
        "(`/api/v1/...`). Obtain a JWT with `POST /api/v1/auth/token`, then use *Authorize*.\n\n"
        "Errors always have the shape `{\"error\": {\"code\", \"message\", \"details\"?}}`."),
    openapi_tags=[
        {"name": "auth", "description": "JWT issuing"},
        {"name": "mobile", "description": "University Mobile App — no auth, 3 calls/student/day (gateway)"},
        {"name": "banking", "description": "Banking App — JWT (student: own account only; bank: any)"},
        {"name": "admin", "description": "University Web Site admin — JWT with role admin"},
    ])


# ---------------------------------------------------------------- errors
def _err(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return _err(e.status, e.code, e.message, e.details)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, e: HTTPException):
    d = e.detail if isinstance(e.detail, dict) else {"code": "HTTP_ERROR", "message": str(e.detail)}
    resp = _err(e.status_code, d.get("code", "HTTP_ERROR"), d.get("message", ""))
    for k, v in (e.headers or {}).items():
        resp.headers[k] = v
    return resp


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    details = [{"field": ".".join(str(p) for p in er["loc"][1:]), "message": er["msg"]} for er in e.errors()]
    return _err(400, "VALIDATION_ERROR", "Request validation failed", details)


def get_conn():
    with db.connect() as conn:
        yield conn


OptTerm = Annotated[str | None, Query(pattern=TERM_PATTERN,
                                      description="If given: balance for that term. If omitted: overall balance across all terms.")]


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok", "service": "tuition-api"}


# ------------------------------------------------------------------ auth
@app.post("/api/v1/auth/token", response_model=TokenResponse, response_model_by_alias=True, tags=["auth"])
def login(body: TokenRequest, conn=Depends(get_conn)):
    row = conn.execute("SELECT password_hash, role FROM app_users WHERE username = %s", (body.username,)).fetchone()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise ApiError(401, "INVALID_CREDENTIALS", "Username or password is incorrect")
    token, ttl = issue_token(body.username, row["role"])
    return TokenResponse(access_token=token, expires_in=ttl, role=row["role"])


# ------------------------------------------------------- university mobile app
@app.get("/api/v1/mobile/tuition/{studentNo}", response_model=TuitionQueryResponse,
         response_model_by_alias=True, tags=["mobile"],
         summary="Query tuition (no auth; gateway limits to 3 calls/student/day)")
def mobile_query(studentNo: str, term: OptTerm = None, conn=Depends(get_conn)):  # noqa: N803 (public name)
    _check_student_no(studentNo)
    return service.query_tuition(conn, studentNo, term)


# ---------------------------------------------------------------- banking app
@app.get("/api/v1/banking/tuition/{studentNo}", response_model=TuitionQueryResponse,
         response_model_by_alias=True, tags=["banking"], summary="Query tuition (auth required)")
def banking_query(studentNo: str, term: OptTerm = None,  # noqa: N803
                  p: Principal = Depends(require_roles("student", "bank")), conn=Depends(get_conn)):
    _check_student_no(studentNo)
    ensure_can_access_student(p, studentNo)
    return service.query_tuition(conn, studentNo, term)


@app.post("/api/v1/banking/payments", response_model=PaymentResponse, response_model_by_alias=True,
          status_code=201, tags=["banking"], summary="Pay tuition (simulated, auth required)")
def banking_pay(body: PaymentRequest,
                x_payment_channel: Annotated[str | None, Header()] = None,
                p: Principal = Depends(require_roles("student", "bank")), conn=Depends(get_conn)):
    ensure_can_access_student(p, body.student_no)
    channel = (x_payment_channel or "banking-app")[:32]
    return service.pay_tuition(conn, student_no=body.student_no, term=body.term,
                               amount=body.amount, transaction_id=body.transaction_id,
                               channel=channel, paid_by=repr(p))


# ---------------------------------------------------------- university admin
@app.post("/api/v1/admin/tuition", response_model=AddTuitionResponse, response_model_by_alias=True,
          response_model_exclude_none=True, tags=["admin"], summary="Create or update tuition for a student/term")
def admin_add(body: AddTuitionRequest, _: Principal = Depends(require_roles("admin")), conn=Depends(get_conn)):
    return service.add_tuition(conn, body)


@app.post("/api/v1/admin/tuition/batch", response_model=BatchResponse, response_model_by_alias=True,
          tags=["admin"], summary="Batch create/update tuition from CSV (studentNo,term,amount,currency)")
async def admin_batch(file: UploadFile = File(...), p: Principal = Depends(require_roles("admin")),
                      conn=Depends(get_conn)):
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise ApiError(413, "FILE_TOO_LARGE", f"CSV must be at most {MAX_CSV_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(400, "INVALID_ENCODING", "CSV must be UTF-8 encoded")
    return service.add_tuition_batch(conn, text, uploaded_by=p.username, file_name=file.filename)


@app.get("/api/v1/admin/tuition/unpaid", response_model=UnpaidPage, response_model_by_alias=True,
         tags=["admin"], summary="Paged list of students with unpaid balance for a term")
def admin_unpaid(term: Annotated[str, Query(pattern=TERM_PATTERN)],
                 page: Annotated[int, Query(ge=1)] = 1,
                 page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
                 _: Principal = Depends(require_roles("admin")), conn=Depends(get_conn)):
    return service.unpaid_tuition(conn, term, page, page_size)


def _check_student_no(student_no: str) -> None:
    if not re.fullmatch(STUDENT_NO_PATTERN, student_no):
        raise ApiError(400, "VALIDATION_ERROR", "studentNo must be 8 digits")
