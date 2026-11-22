"""Loads reproducible test data into PostgreSQL (wipes existing data first).

    python -m tuition_api.seed

Accounts (username / password / role):
    20210001 / student123 / student  2026-FALL paid; 2027-SPRING 6,000 USD, 3,000 paid -> balance 3,000
    20210002 / student123 / student  2026-FALL 4,500 overdue; 2027-SPRING 4,500 unpaid -> balance 9,000
    20210003 / student123 / student  2027-SPRING 200 unpaid (below the $300 installment threshold, Policy §3.1)
    20210004 / student123 / student  2027-SPRING fully paid
    bank     / bank123    / bank     banking-app service client
    admin    / admin123   / admin    university web site admin
Plus 30 students 20220001…20220030 with 2027-SPRING tuition (for paging the unpaid list).
"""
from __future__ import annotations

from decimal import Decimal

import psycopg

from tuition_api import db
from tuition_api.security import hash_password

STUDENTS = [("20210001", "Demo Student One"), ("20210002", "Demo Student Two"),
            ("20210003", "Demo Student Three"), ("20210004", "Demo Student Four")]
USERS = [(sn, "student123", "student", sn, name) for sn, name in STUDENTS] + [
    ("bank", "bank123", "bank", None, "Example Bank Integration"),
    ("admin", "admin123", "admin", None, "Bursar Admin"),
]
TUITION = [
    ("20210001", "2026-FALL", "6000.00"), ("20210001", "2027-SPRING", "6000.00"),
    ("20210002", "2026-FALL", "4500.00"), ("20210002", "2027-SPRING", "4500.00"),
    ("20210003", "2027-SPRING", "200.00"),
    ("20210004", "2027-SPRING", "5200.00"),
]
PAYMENTS = [
    ("SEED-TXN-0001", "20210001", "2026-FALL", "6000.00"),
    ("SEED-TXN-0002", "20210001", "2027-SPRING", "3000.00"),
    ("SEED-TXN-0003", "20210004", "2027-SPRING", "5200.00"),
]
for i in range(1, 31):                 # paging data: every 3rd student has paid in full
    sn = f"2022{i:04d}"
    STUDENTS.append((sn, f"Paging Student {i}"))
    TUITION.append((sn, "2027-SPRING", f"{4000 + 100 * i}.00"))
    if i % 3 == 0:
        PAYMENTS.append((f"SEED-TXN-1{i:03d}", sn, "2027-SPRING", f"{4000 + 100 * i}.00"))
    elif i % 3 == 1:
        PAYMENTS.append((f"SEED-TXN-1{i:03d}", sn, "2027-SPRING", "1000.00"))


_HASHES: dict[str, str] = {}      # PBKDF2 is deliberately slow; hash each demo password once per process


def _hash(pw: str) -> str:
    if pw not in _HASHES:
        _HASHES[pw] = hash_password(pw)
    return _HASHES[pw]


def seed() -> None:
    db.init_db()
    with psycopg.connect(db.database_url(), autocommit=False) as conn:
        conn.execute("TRUNCATE payments, tuition, tuition_batch_imports, app_users, students,"
                     " gateway_rate_counters")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO students (student_no, full_name) VALUES (%s, %s)", STUDENTS)
            cur.executemany("INSERT INTO app_users (username, password_hash, role, student_no, display_name)"
                            " VALUES (%s, %s, %s, %s, %s)",
                            [(u, _hash(pw), r, sn, n) for u, pw, r, sn, n in USERS])
            cur.executemany("INSERT INTO tuition (student_no, term, amount, currency) VALUES (%s, %s, %s, 'USD')",
                            [(sn, t, Decimal(a)) for sn, t, a in TUITION])
            cur.executemany("INSERT INTO payments (transaction_id, student_no, term, amount, channel, paid_by)"
                            " VALUES (%s, %s, %s, %s, 'seed', 'seed')",
                            [(x, sn, t, Decimal(a)) for x, sn, t, a in PAYMENTS])
        conn.commit()
    print(f"Seeded {db.database_url().rsplit('@', 1)[-1]}: {len(USERS)} users, {len(STUDENTS)} students, "
          f"{len(TUITION)} tuition records, {len(PAYMENTS)} payments")


if __name__ == "__main__":
    seed()
