-- Tuition system — PostgreSQL schema (idempotent; applied at service start-up).
-- Money is NUMERIC(12,2): exact decimal arithmetic, never floating point.
-- Balances are DERIVED (tuition.amount - SUM(payments.amount)) instead of stored,
-- so a balance can never drift away from the payment ledger.

CREATE TABLE IF NOT EXISTS students (
    student_no   VARCHAR(8)  PRIMARY KEY CHECK (student_no ~ '^[0-9]{8}$'),
    full_name    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_users (
    username      VARCHAR(64) PRIMARY KEY,               -- = student_no for students
    password_hash TEXT        NOT NULL,                  -- PBKDF2-SHA256
    role          VARCHAR(16) NOT NULL CHECK (role IN ('student', 'bank', 'admin')),
    student_no    VARCHAR(8)  REFERENCES students (student_no),
    display_name  TEXT,
    CHECK ((role = 'student') = (student_no IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS tuition (
    student_no  VARCHAR(8)    NOT NULL REFERENCES students (student_no),
    term        VARCHAR(12)   NOT NULL CHECK (term ~ '^[0-9]{4}-(FALL|SPRING|SUMMER)$'),
    amount      NUMERIC(12,2) NOT NULL CHECK (amount >= 0),
    currency    CHAR(3)       NOT NULL,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (student_no, term)
);

CREATE TABLE IF NOT EXISTS payments (
    transaction_id VARCHAR(64)   PRIMARY KEY,            -- uniqueness = duplicate protection
    student_no     VARCHAR(8)    NOT NULL,
    term           VARCHAR(12)   NOT NULL,
    amount         NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    channel        VARCHAR(32)   NOT NULL,               -- banking-app | ai-assistant | seed
    paid_by        VARCHAR(80)   NOT NULL,               -- authenticated principal
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    FOREIGN KEY (student_no, term) REFERENCES tuition (student_no, term)
);
CREATE INDEX IF NOT EXISTS ix_payments_student_term ON payments (student_no, term);

CREATE TABLE IF NOT EXISTS tuition_batch_imports (
    id           BIGSERIAL   PRIMARY KEY,
    uploaded_by  VARCHAR(64) NOT NULL,
    file_name    TEXT,
    status       VARCHAR(24) NOT NULL,
    total_rows   INT NOT NULL, succeeded INT NOT NULL, failed INT NOT NULL,
    created      INT NOT NULL, updated   INT NOT NULL, unchanged INT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Owned by the API Gateway (mobile "Query Tuition" limit: 3 / student / day).
CREATE TABLE IF NOT EXISTS gateway_rate_counters (
    limit_key   VARCHAR(100) NOT NULL,
    window_day  DATE         NOT NULL,
    count       INT          NOT NULL,
    PRIMARY KEY (limit_key, window_day)
);
