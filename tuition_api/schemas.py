"""Request/response contracts (JSON uses camelCase)."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

STUDENT_NO_PATTERN = r"^\d{8}$"
TERM_PATTERN = r"^\d{4}-(FALL|SPRING|SUMMER)$"
MAX_AMOUNT = Decimal("10000000")


class Api(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _two_decimals(v: Decimal) -> Decimal:
    if v.as_tuple().exponent < -2:
        raise ValueError("amount may have at most 2 decimal places")
    if v > MAX_AMOUNT:
        raise ValueError(f"amount must be <= {MAX_AMOUNT}")
    return v


def money(v: Decimal | int | float) -> float:
    """JSON representation of a NUMERIC(12,2) amount (exact to 2 decimals)."""
    return float(Decimal(v).quantize(Decimal("0.01")))


# ---------- auth ----------
class TokenRequest(Api):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(Api):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    role: str


# ---------- query ----------
class TermBalance(Api):
    term: str
    tuition_total: float
    paid_total: float
    balance: float
    status: Literal["PAID", "PARTIALLY_PAID", "UNPAID"]


class TuitionQueryResponse(Api):
    student_no: str
    scope: Literal["TERM", "OVERALL"]
    term: str | None
    currency: str
    tuition_total: float
    paid_total: float
    balance: float
    terms: list[TermBalance]


# ---------- pay ----------
class PaymentRequest(Api):
    student_no: str = Field(pattern=STUDENT_NO_PATTERN)
    term: str = Field(pattern=TERM_PATTERN)
    amount: Decimal = Field(gt=0)
    transaction_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    _amt = field_validator("amount")(_two_decimals)


class PaymentResponse(Api):
    payment_status: Literal["SUCCESS"]
    coverage: Literal["PARTIAL", "FULL"]
    transaction_id: str
    student_no: str
    term: str
    paid_amount: float
    remaining_balance: float
    currency: str
    paid_at: str


# ---------- admin ----------
class AddTuitionRequest(Api):
    student_no: str = Field(pattern=STUDENT_NO_PATTERN)
    term: str = Field(pattern=TERM_PATTERN)
    amount: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Za-z]{3}$")

    _amt = field_validator("amount")(_two_decimals)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class AddTuitionResponse(Api):
    transaction_status: Literal["CREATED", "UPDATED", "UNCHANGED"]
    student_no: str
    term: str
    amount: float
    currency: str
    previous_amount: float | None = None


class BatchRowError(Api):
    line: int
    student_no: str | None = None
    term: str | None = None
    code: str
    message: str


class BatchResponse(Api):
    transaction_status: Literal["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"]
    total_rows: int
    succeeded: int
    failed: int
    created: int
    updated: int
    unchanged: int
    errors: list[BatchRowError]


class UnpaidItem(Api):
    student_no: str
    tuition_total: float
    paid_total: float
    balance: float
    currency: str


class UnpaidPage(Api):
    term: str
    page: int
    page_size: int
    total_items: int
    total_pages: int
    items: list[UnpaidItem]
