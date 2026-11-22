"""Unit tests for every REST operation of the Tuition API.
Each operation has success cases and failure/edge cases (validation, auth,
business rules)."""
import threading
from decimal import Decimal

from conftest import token

from tuition_api import db, service

SPRING = "2027-SPRING"


class TestAuthToken:
    def test_valid_credentials_issue_jwt(self, api):
        r = api.post("/api/v1/auth/token", json={"username": "20210001", "password": "student123"})
        assert r.status_code == 200
        assert r.json()["tokenType"] == "Bearer" and r.json()["role"] == "student"

    def test_wrong_password_is_rejected(self, api):
        r = api.post("/api/v1/auth/token", json={"username": "20210001", "password": "wrong"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"

    def test_missing_field_is_validation_error(self, api):
        r = api.post("/api/v1/auth/token", json={"username": "20210001"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR"


class TestMobileQueryTuition:
    def test_overall_balance_has_per_term_breakdown(self, api):
        body = api.get("/api/v1/mobile/tuition/20210002").json()
        assert body["scope"] == "OVERALL" and body["term"] is None
        assert (body["tuitionTotal"], body["paidTotal"], body["balance"]) == (9000.0, 0.0, 9000.0)
        assert [t["term"] for t in body["terms"]] == ["2026-FALL", SPRING]

    def test_term_balance(self, api):
        body = api.get("/api/v1/mobile/tuition/20210001", params={"term": SPRING}).json()
        assert body == {"studentNo": "20210001", "scope": "TERM", "term": SPRING, "currency": "USD",
                        "tuitionTotal": 6000.0, "paidTotal": 3000.0, "balance": 3000.0,
                        "terms": [{"term": SPRING, "tuitionTotal": 6000.0, "paidTotal": 3000.0,
                                   "balance": 3000.0, "status": "PARTIALLY_PAID"}]}

    def test_unknown_student_404(self, api):
        r = api.get("/api/v1/mobile/tuition/99999999")
        assert r.status_code == 404 and r.json()["error"]["code"] == "TUITION_NOT_FOUND"

    def test_invalid_student_no_and_term_400(self, api):
        assert api.get("/api/v1/mobile/tuition/abc").json()["error"]["code"] == "VALIDATION_ERROR"
        assert api.get("/api/v1/mobile/tuition/20210001", params={"term": "fall"}).status_code == 400

    def test_term_without_record_404(self, api):
        assert api.get("/api/v1/mobile/tuition/20210003", params={"term": "2026-FALL"}).status_code == 404


class TestBankingQueryTuition:
    def test_student_reads_own_account(self, api):
        h = token(api, "20210001", "student123")
        assert api.get("/api/v1/banking/tuition/20210001", headers=h).json()["balance"] == 3000.0

    def test_bank_client_reads_any_account(self, api):
        h = token(api, "bank", "bank123")
        assert api.get("/api/v1/banking/tuition/20210002", headers=h).status_code == 200

    def test_without_token_401(self, api):
        assert api.get("/api/v1/banking/tuition/20210001").status_code == 401

    def test_student_cannot_read_other_student_403(self, api):
        h = token(api, "20210001", "student123")
        r = api.get("/api/v1/banking/tuition/20210002", headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"

    def test_admin_role_not_allowed_and_bad_token(self, api):
        assert api.get("/api/v1/banking/tuition/20210002", headers=token(api, "admin", "admin123")).status_code == 403
        r = api.get("/api/v1/banking/tuition/20210001", headers={"Authorization": "Bearer garbage"})
        assert r.json()["error"]["code"] == "INVALID_TOKEN"


def _pay(api, h, amount, txn, sn="20210001", term=SPRING):
    return api.post("/api/v1/banking/payments", headers=h,
                    json={"studentNo": sn, "term": term, "amount": amount, "transactionId": txn})


class TestPayTuition:
    def test_partial_then_full_payment(self, api):
        h = token(api, "20210001", "student123")
        r = _pay(api, h, 1000, "TXN-00000001")
        assert r.status_code == 201
        assert (r.json()["paymentStatus"], r.json()["coverage"], r.json()["paidAmount"],
                r.json()["remainingBalance"]) == ("SUCCESS", "PARTIAL", 1000.0, 2000.0)
        full = _pay(api, h, "2000.00", "TXN-00000002").json()
        assert full["coverage"] == "FULL" and full["remainingBalance"] == 0.0
        assert api.get("/api/v1/banking/tuition/20210001", headers=h).json()["terms"][1]["status"] == "PAID"

    def test_duplicate_transaction_id_rejected_with_original(self, api):
        h = token(api, "20210001", "student123")
        _pay(api, h, 100, "TXN-DUP-0001")
        r = _pay(api, h, 100, "TXN-DUP-0001")
        assert r.status_code == 409 and r.json()["error"]["code"] == "DUPLICATE_TRANSACTION"
        assert r.json()["error"]["details"]["originalTransaction"]["amount"] == 100.0
        assert api.get("/api/v1/banking/tuition/20210001", headers=h).json()["balance"] == 2900.0

    def test_amount_exceeding_balance_rejected(self, api):
        h = token(api, "20210001", "student123")
        r = _pay(api, h, "3000.01", "TXN-00000003")
        assert r.status_code == 422 and r.json()["error"]["details"]["outstandingBalance"] == 3000.0
        r = _pay(api, h, 1, "TXN-00000004", term="2026-FALL")           # fully paid term
        assert r.json()["error"]["code"] == "AMOUNT_EXCEEDS_BALANCE"

    def test_invalid_amounts_and_ids(self, api):
        h = token(api, "20210001", "student123")
        assert _pay(api, h, "10.001", "TXN-00000005").status_code == 400
        assert _pay(api, h, 0, "TXN-00000006").status_code == 400
        assert _pay(api, h, -5, "TXN-00000007").status_code == 400
        assert _pay(api, h, 10, "short").status_code == 400

    def test_authorization(self, api):
        h = token(api, "20210001", "student123")
        assert _pay(api, h, 10, "TXN-00000008", sn="20210002").status_code == 403   # someone else
        assert _pay(api, {}, 10, "TXN-00000009").status_code == 401
        bank = token(api, "bank", "bank123")
        assert _pay(api, bank, 10, "TXN-00000010", sn="20210002").status_code == 201

    def test_unknown_term_404(self, api):
        h = token(api, "20210001", "student123")
        assert _pay(api, h, 10, "TXN-00000011", term="2028-FALL").status_code == 404


def test_concurrent_payments_cannot_exceed_balance():
    """20 parallel payments of 500 against a 3,000 balance -> exactly 6 succeed."""
    results = []

    def worker(i):
        with db.connect() as conn:
            try:
                service.pay_tuition(conn, student_no="20210001", term=SPRING, amount=Decimal("500"),
                                    transaction_id=f"PAR-{i:04d}", channel="test", paid_by="test")
                results.append("ok")
            except service.ApiError as e:
                results.append(e.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 6 and results.count("AMOUNT_EXCEEDS_BALANCE") == 14
    with db.connect() as conn:
        assert service.query_tuition(conn, "20210001", SPRING)["balance"] == 0.0


def test_concurrent_duplicate_transaction_recorded_once():
    results = []

    def worker():
        with db.connect() as conn:
            try:
                service.pay_tuition(conn, student_no="20210002", term=SPRING, amount=Decimal("10"),
                                    transaction_id="SAME-TXN-ID", channel="test", paid_by="test")
                results.append("ok")
            except service.ApiError as e:
                results.append(e.code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("DUPLICATE_TRANSACTION") == 7


def _add(api, h, sn, term, amt, cur="USD"):
    return api.post("/api/v1/admin/tuition", headers=h,
                    json={"studentNo": sn, "term": term, "amount": amt, "currency": cur})


class TestAddTuition:
    def test_create_unchanged_update(self, api):
        h = token(api, "admin", "admin123")
        assert _add(api, h, "20229999", "2026-FALL", 5000).json()["transactionStatus"] == "CREATED"
        assert _add(api, h, "20229999", "2026-FALL", "5000.00").json()["transactionStatus"] == "UNCHANGED"
        upd = _add(api, h, "20229999", "2026-FALL", 5500).json()
        assert upd["transactionStatus"] == "UPDATED" and upd["previousAmount"] == 5000.0
        assert api.get("/api/v1/mobile/tuition/20229999").json()["balance"] == 5500.0

    def test_currency_mismatch_rejected(self, api):
        h = token(api, "admin", "admin123")
        r = _add(api, h, "20210001", "2027-SUMMER", 100, "EUR")
        assert r.status_code == 409 and r.json()["error"]["code"] == "CURRENCY_MISMATCH"

    def test_amount_below_paid_rejected(self, api):
        h = token(api, "admin", "admin123")
        r = _add(api, h, "20210001", SPRING, 2000)                       # 3,000 already paid
        assert r.status_code == 409 and r.json()["error"]["code"] == "AMOUNT_BELOW_PAID"
        assert _add(api, h, "20210001", SPRING, 3000).json()["transactionStatus"] == "UPDATED"

    def test_validation_and_auth(self, api):
        h = token(api, "admin", "admin123")
        assert _add(api, h, "123", SPRING, 10).status_code == 400
        assert _add(api, h, "20210001", "2027-WINTER", 10).status_code == 400
        assert _add(api, h, "20210001", SPRING, -1).status_code == 400
        assert _add(api, h, "20210001", SPRING, 10, "US").status_code == 400
        assert _add(api, token(api, "20210001", "student123"), "20210001", SPRING, 1).status_code == 403
        assert _add(api, {}, "20210001", SPRING, 1).status_code == 401


class TestAddTuitionBatch:
    def test_mixed_file_reports_counts_and_row_errors(self, api):
        h = token(api, "admin", "admin123")
        csv_text = ("studentNo,term,amount,currency\n"
                    "20230001,2027-SPRING,5000,USD\n"
                    "20230002,2027-SPRING,4000.50,usd\n"
                    "20230001,2027-SPRING,9999,USD\n"     # duplicate in file
                    "2023,2027-SPRING,100,USD\n"           # bad student no
                    "20210001,2027-SPRING,6000,USD\n"      # unchanged
                    "20210002,2027-SPRING,abc,USD\n"       # bad amount
                    "20210002,2026-FALL,4600,TRY\n"        # currency mismatch
                    "20230003,2027-SPRING\n"               # wrong column count
                    "20210003,2027-SPRING,250,USD\n"       # updated
                    "\n")
        r = api.post("/api/v1/admin/tuition/batch", headers=h, files={"file": ("t.csv", csv_text, "text/csv")})
        body = r.json()
        assert r.status_code == 200, body
        assert (body["totalRows"], body["succeeded"], body["failed"]) == (9, 4, 5)
        assert (body["created"], body["updated"], body["unchanged"]) == (2, 1, 1)
        assert body["transactionStatus"] == "COMPLETED_WITH_ERRORS"
        assert {e["line"]: e["code"] for e in body["errors"]} == {
            4: "DUPLICATE_IN_FILE", 5: "VALIDATION_ERROR", 7: "VALIDATION_ERROR",
            8: "CURRENCY_MISMATCH", 9: "INVALID_ROW"}
        assert api.get("/api/v1/mobile/tuition/20230002").json()["balance"] == 4000.5
        with db.connect() as c:
            log = c.execute("SELECT * FROM tuition_batch_imports").fetchone()
        assert (log["uploaded_by"], log["succeeded"], log["failed"]) == ("admin", 4, 5)

    def test_all_rows_valid_is_completed(self, api):
        h = token(api, "admin", "admin123")
        r = api.post("/api/v1/admin/tuition/batch", headers=h, files={"file": (
            "t.csv", "studentNo,term,amount,currency\n20230009,2027-SPRING,100,USD\n", "text/csv")})
        assert r.json()["transactionStatus"] == "COMPLETED" and r.json()["created"] == 1

    def test_invalid_header_empty_file_and_auth(self, api):
        h = token(api, "admin", "admin123")
        bad = api.post("/api/v1/admin/tuition/batch", headers=h, files={"file": ("t.csv", "id,term\n1,2\n", "text/csv")})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_HEADER"
        empty = api.post("/api/v1/admin/tuition/batch", headers=h, files={"file": ("t.csv", "", "text/csv")})
        assert empty.json()["error"]["code"] == "EMPTY_FILE"
        only_bad = api.post("/api/v1/admin/tuition/batch", headers=h, files={"file": (
            "t.csv", "studentNo,term,amount,currency\nx,y,z,w\n", "text/csv")})
        assert only_bad.json()["transactionStatus"] == "FAILED"
        assert api.post("/api/v1/admin/tuition/batch", headers=token(api, "bank", "bank123"),
                        files={"file": ("t.csv", "x", "text/csv")}).status_code == 403


class TestUnpaidTuitionStatus:
    def test_paging(self, api):
        h = token(api, "admin", "admin123")
        p1 = api.get("/api/v1/admin/tuition/unpaid", headers=h,
                     params={"term": SPRING, "page": 1, "pageSize": 10}).json()
        # 3 demo students with balance + 20 of the 30 paging students (every 3rd paid in full)
        assert (p1["totalItems"], p1["totalPages"], len(p1["items"])) == (23, 3, 10)
        assert p1["items"][0] == {"studentNo": "20210001", "tuitionTotal": 6000.0, "paidTotal": 3000.0,
                                  "balance": 3000.0, "currency": "USD"}
        p3 = api.get("/api/v1/admin/tuition/unpaid", headers=h,
                     params={"term": SPRING, "page": 3, "pageSize": 10}).json()
        assert len(p3["items"]) == 3
        listed = {i["studentNo"] for i in p1["items"] + p3["items"]}
        assert "20210004" not in listed and "20220003" not in listed      # fully paid -> excluded

    def test_page_beyond_end_is_empty(self, api):
        h = token(api, "admin", "admin123")
        r = api.get("/api/v1/admin/tuition/unpaid", headers=h, params={"term": SPRING, "page": 99}).json()
        assert r["items"] == [] and r["totalItems"] == 23

    def test_invalid_paging_and_auth(self, api):
        h = token(api, "admin", "admin123")
        assert api.get("/api/v1/admin/tuition/unpaid", headers=h, params={"term": SPRING, "pageSize": 500}).status_code == 400
        assert api.get("/api/v1/admin/tuition/unpaid", headers=h, params={"term": SPRING, "page": 0}).status_code == 400
        assert api.get("/api/v1/admin/tuition/unpaid", headers=h).status_code == 400          # term required
        assert api.get("/api/v1/admin/tuition/unpaid", params={"term": SPRING}).status_code == 401
        assert api.get("/api/v1/admin/tuition/unpaid", headers=token(api, "20210001", "student123"),
                       params={"term": SPRING}).status_code == 403
