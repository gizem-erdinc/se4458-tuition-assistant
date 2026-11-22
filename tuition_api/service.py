"""Business rules of the Tuition service (framework-independent, PostgreSQL)."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import psycopg
from psycopg import errors as pgerr
from pydantic import ValidationError

from tuition_api.schemas import AddTuitionRequest, money

MAX_BATCH_ROWS = 10_000
BATCH_HEADER = ["studentNo", "term", "amount", "currency"]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


# ------------------------------------------------------------------ queries
_TERM_SQL = """
    SELECT t.term, t.amount, t.currency,
           COALESCE((SELECT SUM(p.amount) FROM payments p
                     WHERE p.student_no = t.student_no AND p.term = t.term), 0) AS paid
    FROM tuition t WHERE t.student_no = %s {extra} ORDER BY t.term"""


def _status(amount: Decimal, paid: Decimal) -> str:
    if paid >= amount:
        return "PAID"
    return "PARTIALLY_PAID" if paid > 0 else "UNPAID"


def query_tuition(conn: psycopg.Connection, student_no: str, term: str | None) -> dict:
    if term:
        rows = conn.execute(_TERM_SQL.format(extra="AND t.term = %s"), (student_no, term)).fetchall()
    else:
        rows = conn.execute(_TERM_SQL.format(extra=""), (student_no,)).fetchall()
    if not rows:
        what = f"term {term}" if term else "any term"
        raise ApiError(404, "TUITION_NOT_FOUND", f"No tuition record for student {student_no} in {what}")
    total = sum(r["amount"] for r in rows)
    paid = sum(r["paid"] for r in rows)
    return {
        "student_no": student_no,
        "scope": "TERM" if term else "OVERALL",
        "term": term,
        "currency": rows[0]["currency"],
        "tuition_total": money(total),
        "paid_total": money(paid),
        "balance": money(total - paid),
        "terms": [{"term": r["term"], "tuition_total": money(r["amount"]), "paid_total": money(r["paid"]),
                   "balance": money(r["amount"] - r["paid"]), "status": _status(r["amount"], r["paid"])}
                  for r in rows],
    }


# ----------------------------------------------------------------- payments
def pay_tuition(conn: psycopg.Connection, *, student_no: str, term: str, amount: Decimal,
                transaction_id: str, channel: str, paid_by: str) -> dict:
    """Records a simulated payment.

    The tuition row is locked with ``SELECT … FOR UPDATE`` so concurrent payments
    for the same student/term are serialized and can never jointly exceed the
    balance. ``payments.transaction_id`` is the primary key, so a duplicate can
    never be stored even across different students."""
    try:
        with conn.transaction():
            prior = conn.execute("SELECT student_no, term, amount, created_at FROM payments"
                                 " WHERE transaction_id = %s", (transaction_id,)).fetchone()
            if prior:
                raise _duplicate(transaction_id, prior)
            row = conn.execute("SELECT amount, currency FROM tuition WHERE student_no = %s AND term = %s"
                               " FOR UPDATE", (student_no, term)).fetchone()
            if row is None:
                raise ApiError(404, "TUITION_NOT_FOUND",
                               f"No tuition record for student {student_no} in term {term}")
            paid = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM payments"
                                " WHERE student_no = %s AND term = %s", (student_no, term)).fetchone()["s"]
            balance = row["amount"] - paid
            if amount > balance:
                raise ApiError(422, "AMOUNT_EXCEEDS_BALANCE",
                               f"Amount {money(amount)} exceeds outstanding balance {money(balance)}",
                               {"outstandingBalance": money(balance), "currency": row["currency"]})
            created = conn.execute(
                "INSERT INTO payments (transaction_id, student_no, term, amount, channel, paid_by)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING created_at",
                (transaction_id, student_no, term, amount, channel, paid_by)).fetchone()["created_at"]
    except pgerr.UniqueViolation:       # lost a race with an identical transaction ID
        prior = conn.execute("SELECT student_no, term, amount, created_at FROM payments"
                             " WHERE transaction_id = %s", (transaction_id,)).fetchone()
        raise _duplicate(transaction_id, prior)
    remaining = balance - amount
    return {
        "payment_status": "SUCCESS",
        "coverage": "FULL" if remaining == 0 else "PARTIAL",
        "transaction_id": transaction_id,
        "student_no": student_no,
        "term": term,
        "paid_amount": money(amount),
        "remaining_balance": money(remaining),
        "currency": row["currency"],
        "paid_at": created.isoformat(timespec="seconds"),
    }


def _duplicate(transaction_id: str, prior: dict | None) -> ApiError:
    """Rejects the retry but tells the caller what the original transaction did,
    so a client that timed out can learn the outcome without paying twice."""
    details = None
    if prior:
        details = {"originalTransaction": {"studentNo": prior["student_no"], "term": prior["term"],
                                           "amount": money(prior["amount"]),
                                           "recordedAt": prior["created_at"].isoformat(timespec="seconds")}}
    return ApiError(409, "DUPLICATE_TRANSACTION",
                    f"Transaction {transaction_id} was already processed; it was not recorded again", details)


# -------------------------------------------------------------------- admin
def _upsert(conn: psycopg.Connection, req: AddTuitionRequest) -> dict:
    """Duplicate-handling rules for (studentNo, term):

    1. No record                          -> insert            (CREATED)
    2. Same amount and currency           -> no-op             (UNCHANGED)
    3. Different amount, same currency    -> overwrite amount  (UPDATED, previousAmount returned)
    4. Currency differs from ANY existing record of the student -> 409 CURRENCY_MISMATCH
       (a student's balance is always in one currency; no implicit FX)
    5. New amount lower than what is already paid for that term -> 409 AMOUNT_BELOW_PAID
    Unknown student numbers are registered in ``students`` automatically.
    """
    amount = req.amount.quantize(Decimal("0.01"))
    conn.execute("INSERT INTO students (student_no) VALUES (%s) ON CONFLICT DO NOTHING", (req.student_no,))
    currencies = {r["currency"] for r in conn.execute(
        "SELECT DISTINCT currency FROM tuition WHERE student_no = %s", (req.student_no,)).fetchall()}
    if currencies and currencies != {req.currency}:
        raise ApiError(409, "CURRENCY_MISMATCH",
                       f"Student {req.student_no} is billed in {', '.join(sorted(currencies))}, not {req.currency}")
    existing = conn.execute("SELECT amount FROM tuition WHERE student_no = %s AND term = %s FOR UPDATE",
                            (req.student_no, req.term)).fetchone()
    base = {"student_no": req.student_no, "term": req.term, "amount": money(amount), "currency": req.currency}
    if existing is None:
        conn.execute("INSERT INTO tuition (student_no, term, amount, currency) VALUES (%s, %s, %s, %s)",
                     (req.student_no, req.term, amount, req.currency))
        return {"transaction_status": "CREATED", **base}
    if existing["amount"] == amount:
        return {"transaction_status": "UNCHANGED", **base, "previous_amount": money(amount)}
    paid = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE student_no = %s AND term = %s",
                        (req.student_no, req.term)).fetchone()["s"]
    if amount < paid:
        raise ApiError(409, "AMOUNT_BELOW_PAID",
                       f"New amount {money(amount)} is lower than the already paid {money(paid)}")
    conn.execute("UPDATE tuition SET amount = %s, updated_at = now() WHERE student_no = %s AND term = %s",
                 (amount, req.student_no, req.term))
    return {"transaction_status": "UPDATED", **base, "previous_amount": money(existing["amount"])}


def add_tuition(conn: psycopg.Connection, req: AddTuitionRequest) -> dict:
    with conn.transaction():
        return _upsert(conn, req)


def add_tuition_batch(conn: psycopg.Connection, csv_text: str, uploaded_by: str = "admin",
                      file_name: str | None = None) -> dict:
    """Rows are processed independently (one SAVEPOINT per row): a bad row is
    reported and skipped, good rows are committed. A (studentNo, term) pair that
    appears twice in the same file is applied once; later occurrences are
    rejected as DUPLICATE_IN_FILE so the outcome never depends on silent
    last-row-wins."""
    reader = csv.reader(io.StringIO(csv_text.lstrip("﻿")))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise ApiError(400, "EMPTY_FILE", "CSV file is empty")
    if header != BATCH_HEADER:
        raise ApiError(400, "INVALID_HEADER", f"CSV header must be: {','.join(BATCH_HEADER)}",
                       {"received": header})

    counts = {"created": 0, "updated": 0, "unchanged": 0}
    errors: list[dict] = []
    seen: dict[tuple[str, str], int] = {}
    total = 0
    with conn.transaction():
        for line_no, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue
            total += 1
            if total > MAX_BATCH_ROWS:
                raise ApiError(413, "TOO_MANY_ROWS", f"Batch limited to {MAX_BATCH_ROWS} rows")
            cells = [c.strip() for c in row]
            sn, term = (cells + [None, None])[:2]
            if len(cells) != len(BATCH_HEADER):
                errors.append({"line": line_no, "student_no": sn, "term": term, "code": "INVALID_ROW",
                               "message": f"Expected {len(BATCH_HEADER)} columns, got {len(cells)}"})
                continue
            try:
                req = AddTuitionRequest.model_validate(dict(zip(BATCH_HEADER, cells)))
            except ValidationError as e:
                msg = "; ".join(f"{'.'.join(map(str, er['loc']))}: {er['msg']}" for er in e.errors())
                errors.append({"line": line_no, "student_no": sn, "term": term,
                               "code": "VALIDATION_ERROR", "message": msg})
                continue
            key = (req.student_no, req.term)
            if key in seen:
                errors.append({"line": line_no, "student_no": sn, "term": term, "code": "DUPLICATE_IN_FILE",
                               "message": f"Same studentNo/term already processed on line {seen[key]}"})
                continue
            seen[key] = line_no
            try:
                with conn.transaction():                       # SAVEPOINT for this row
                    res = _upsert(conn, req)
                counts[res["transaction_status"].lower()] += 1
            except ApiError as e:
                errors.append({"line": line_no, "student_no": sn, "term": term,
                               "code": e.code, "message": e.message})

        succeeded = sum(counts.values())
        status = "COMPLETED" if not errors else "FAILED" if succeeded == 0 else "COMPLETED_WITH_ERRORS"
        conn.execute(
            "INSERT INTO tuition_batch_imports (uploaded_by, file_name, status, total_rows, succeeded, failed,"
            " created, updated, unchanged) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (uploaded_by, file_name, status, total, succeeded, len(errors),
             counts["created"], counts["updated"], counts["unchanged"]))
    return {"transaction_status": status, "total_rows": total, "succeeded": succeeded,
            "failed": len(errors), **counts, "errors": errors}


def unpaid_tuition(conn: psycopg.Connection, term: str, page: int, page_size: int) -> dict:
    base = """
        FROM tuition t
        LEFT JOIN (SELECT student_no, term, SUM(amount) AS paid
                   FROM payments GROUP BY student_no, term) p
          ON p.student_no = t.student_no AND p.term = t.term
        WHERE t.term = %s AND t.amount - COALESCE(p.paid, 0) > 0"""
    total = conn.execute(f"SELECT COUNT(*) AS n {base}", (term,)).fetchone()["n"]
    rows = conn.execute(
        f"SELECT t.student_no, t.amount, t.currency, COALESCE(p.paid, 0) AS paid {base}"
        " ORDER BY t.student_no LIMIT %s OFFSET %s", (term, page_size, (page - 1) * page_size)).fetchall()
    return {
        "term": term, "page": page, "page_size": page_size,
        "total_items": total, "total_pages": (total + page_size - 1) // page_size,
        "items": [{"student_no": r["student_no"], "tuition_total": money(r["amount"]),
                   "paid_total": money(r["paid"]), "balance": money(r["amount"] - r["paid"]),
                   "currency": r["currency"]} for r in rows],
    }
