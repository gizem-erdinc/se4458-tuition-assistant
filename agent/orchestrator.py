"""AI Agent orchestrator.

    user question -> LLM selects tools -> agent executes MCP and/or REST calls
    -> tool results go back to the LLM -> grounded answer (with policy sections)

Guarantees enforced in CODE (not left to the prompt):
* The LLM never calls a backend itself; it only *requests* tools. The agent
  validates arguments and executes them.
* Account identity comes from the authenticated session. Tools have no
  ``studentNo`` parameter, so the LLM cannot ask for another student's data;
  the REST API additionally enforces ownership on the JWT.
* ``pay_tuition`` is a two-phase action. The LLM's call only *prepares* a
  payment (balance pre-check, transaction ID generated); the payment API is
  called exclusively from ``resolve_confirmation`` after the human explicitly
  approves. The pending action is single-use, bound to the session and
  expires after ``CONFIRMATION_TTL``.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from agent.backends import GatewayError, PolicyMcpClient, TuitionGatewayClient
from agent.llm import LLMClient, ToolCall, ToolSpec

CONFIRMATION_TTL = timedelta(minutes=5)
TERM_RE = re.compile(r"^\d{4}-(FALL|SPRING|SUMMER)$")
MAX_TOOL_RESULT_CHARS = 6000

# Formats are validated by the agent code (TERM_RE, amount checks), not with JSON-Schema
# "pattern"/"minimum": some providers (e.g. Groq) enforce schema keywords server-side and
# reject a whole model turn when the model sends "" for an optional field.
REST_TOOLS = [
    ToolSpec(
        name="get_tuition_balance",
        description=(
            "Live account data from the Tuition REST API (via API Gateway) for the LOGGED-IN student: "
            "tuition amount, amount paid and outstanding balance. Omit `term` to get the overall balance "
            "across all terms with a per-term breakdown. The student number is taken from the "
            "authenticated session automatically. Contains no policy rules."),
        parameters={"type": "object", "properties": {
            "term": {"type": "string",
                     "description": "Optional term code in the form YYYY-FALL, YYYY-SPRING or YYYY-SUMMER "
                                    "(e.g. 2027-SPRING). Omit the field entirely for the overall balance."}},
            "required": []},
    ),
    ToolSpec(
        name="pay_tuition",
        description=(
            "Prepare a simulated tuition payment for the logged-in student. Calling this does NOT move "
            "money: the system shows the student a confirmation dialog and only pays after they approve. "
            "Call it only when the student explicitly asked to pay AND both the term and the exact "
            "amount are known; if either is unclear, ask the student instead of guessing."),
        parameters={"type": "object", "properties": {
            "term": {"type": "string", "description": "Term code YYYY-FALL|SPRING|SUMMER, e.g. 2027-SPRING"},
            "amount": {"type": "number",
                       "description": "Positive amount to pay in the account currency (max 2 decimals)"}},
            "required": ["term", "amount"]},
    ),
]


def system_prompt(student_no: str, today: str) -> str:
    return f"""You are the University Tuition Assistant of Aegean Coast University.
Today is {today}. You are talking to the authenticated student {student_no}.

Tools:
- search_tuition_policies (MCP): the official tuition policy document — deadlines, installments,
  late fees, holds, scholarships, refunds, partial payments, bank transfer instructions.
- get_tuition_balance (REST API): this student's live tuition, payments and balance.
- pay_tuition (REST API): prepares a payment; the student must confirm it in the UI.

Rules:
1. Rules/procedures -> search_tuition_policies. Personal numbers -> get_tuition_balance.
   Questions that apply a rule to the student's own situation (e.g. "can I pay my balance in
   installments?") need BOTH; call them in the same step when possible.
2. Answer only from tool results. Never invent amounts, dates, rules, offices, portals, contact
   channels or procedures that do not appear in a tool result. If the policy search returns nothing
   relevant, say that the policy document does not cover it.
3. Cite policy facts with their section, e.g. "(Policy §3.2)". Show money with its currency.
4. When applying a policy to the account, check each condition against the live data and state
   which are satisfied and which cannot be verified from the available data (e.g. approvals,
   exceptions, scholarship eligibility). Never state or imply that something is approved unless a
   tool result says so.
5. Payments: a question such as "Can I pay $500?" is not a payment request. Call pay_tuition only
   when the student clearly asks you to pay; if the amount or term is unclear, ask first. Never say a
   payment was made unless the pay_tuition result has paymentStatus SUCCESS.
   The student's data belongs only to the logged-in student; never look up another student.
6. Do not add generic "next steps" or advice about where or how to apply (no portals, offices,
   forms or e-mails) unless that exact procedure appears in a tool result; if the documents do not
   say how to request something, say that the policy does not describe the procedure.
7. Reply in the language the student writes in. Be concise.
"""


@dataclass
class TraceEvent:
    step: int
    type: str                    # LLM | MCP | REST via Gateway | CONFIRMATION | GUARD
    name: str
    input: Any = None
    output: Any = None
    status: str = "ok"
    ms: float = 0.0
    endpoint: str | None = None


@dataclass
class PendingPayment:
    action_id: str
    tool_call_id: str
    term: str
    amount: float
    currency: str
    balance_before: float
    transaction_id: str
    expires_at: datetime

    def public(self, student_no: str) -> dict:
        return {"actionId": self.action_id, "type": "PAYMENT", "studentNo": student_no, "term": self.term,
                "amount": self.amount, "currency": self.currency, "balanceBefore": self.balance_before,
                "balanceAfter": round(self.balance_before - self.amount, 2),
                "transactionId": self.transaction_id, "expiresAt": self.expires_at.isoformat(timespec="seconds")}


@dataclass
class ChatSession:
    session_id: str
    student_no: str
    token: str
    history: list[dict] = field(default_factory=list)
    pending: PendingPayment | None = None


@dataclass
class AgentTurn:
    reply: str
    trace: list[dict]
    pending_action: dict | None = None


class TuitionAgent:
    def __init__(self, llm: LLMClient, mcp: PolicyMcpClient, gateway: TuitionGatewayClient,
                 max_steps: int = 6, today: str | None = None):
        self.llm, self.mcp, self.gateway = llm, mcp, gateway
        self.max_steps = max_steps
        self.today = today
        self._mcp_tool_names = {t.name for t in mcp.tools}

    # ------------------------------------------------------------ tool catalogue
    def tool_specs(self) -> list[ToolSpec]:
        """MCP tools are discovered from the MCP server at runtime (not hard-coded)."""
        mcp_specs = [ToolSpec(t.name, t.description or "", t.input_schema) for t in self.mcp.tools]
        return mcp_specs + REST_TOOLS

    # ----------------------------------------------------------------- sessions
    async def login(self, student_no: str, password: str) -> ChatSession:
        tok = await self.gateway.login(student_no, password)
        if tok.get("role") != "student":
            raise GatewayError(403, {"error": {"code": "FORBIDDEN", "message": "Assistant is for students"}})
        return ChatSession(uuid.uuid4().hex, student_no, tok["accessToken"])

    async def chat(self, s: ChatSession, message: str) -> AgentTurn:
        trace: list[dict] = []
        if s.pending:   # user moved on without confirming -> payment is abandoned, not executed
            self._tool_result(s, s.pending.tool_call_id, "pay_tuition",
                              {"status": "NOT_CONFIRMED", "charged": False,
                               "message": "Student did not confirm; payment was not executed."})
            self._trace(trace, "CONFIRMATION", "pay_tuition", status="abandoned",
                        output={"actionId": s.pending.action_id, "charged": False})
            s.pending = None
        s.history.append({"role": "user", "content": message})
        return await self._loop(s, trace)

    async def resolve_confirmation(self, s: ChatSession, action_id: str, approve: bool) -> AgentTurn:
        trace: list[dict] = []
        p = s.pending
        if p is None or p.action_id != action_id:
            return AgentTurn("There is no pending payment with that ID (it may already be handled).", trace)
        s.pending = None                                     # single use
        if datetime.now(timezone.utc) > p.expires_at:
            self._tool_result(s, p.tool_call_id, "pay_tuition",
                              {"status": "CONFIRMATION_EXPIRED", "charged": False})
            self._trace(trace, "CONFIRMATION", "pay_tuition", status="expired", output=p.public(s.student_no))
        elif not approve:
            self._tool_result(s, p.tool_call_id, "pay_tuition",
                              {"status": "CANCELLED_BY_STUDENT", "charged": False})
            self._trace(trace, "CONFIRMATION", "pay_tuition", status="rejected", output=p.public(s.student_no))
        else:
            self._trace(trace, "CONFIRMATION", "pay_tuition", status="approved", output=p.public(s.student_no))
            t0 = time.perf_counter()
            status, body = await self.gateway.pay_tuition(s.token, s.student_no, p.term, p.amount, p.transaction_id)
            self._trace(trace, "REST via Gateway", "pay_tuition",
                        input={"studentNo": s.student_no, "term": p.term, "amount": p.amount,
                               "transactionId": p.transaction_id},
                        output=body, status=str(status), ms=_ms(t0), endpoint="POST /api/v1/banking/payments")
            self._tool_result(s, p.tool_call_id, "pay_tuition", body)
        return await self._loop(s, trace)

    # --------------------------------------------------------------- main loop
    async def _loop(self, s: ChatSession, trace: list[dict]) -> AgentTurn:
        tools = self.tool_specs()
        system = system_prompt(s.student_no, self.today or datetime.now().strftime("%Y-%m-%d"))
        for _ in range(self.max_steps):
            t0 = time.perf_counter()
            try:
                resp = await self.llm.complete(system, s.history, tools)
            except Exception as e:  # provider/network error -> visible, not swallowed
                self._trace(trace, "LLM", self.llm.model, status="error", output=str(e), ms=_ms(t0))
                return AgentTurn(f"The language model is unavailable right now ({type(e).__name__}).", trace)
            self._trace(trace, "LLM", self.llm.model, ms=_ms(t0),
                        output={"selected_tools": [{"name": c.name, "arguments": c.arguments}
                                                   for c in resp.tool_calls] or "none (final answer)",
                                "usage": resp.usage})
            s.history.append({"role": "assistant", "content": resp.text, "tool_calls": resp.tool_calls})
            if not resp.tool_calls:
                return AgentTurn(resp.text, trace)

            for call in resp.tool_calls:
                if call.name == "pay_tuition" and s.pending is None:
                    result = await self._prepare_payment(s, call, trace)
                    if result is not None:          # validation failure -> back to the LLM
                        self._tool_result(s, call.id, call.name, result)
                elif call.name == "pay_tuition":
                    self._tool_result(s, call.id, call.name,
                                      {"error": "ONLY_ONE_PAYMENT", "message": "Only one payment at a time."})
                else:
                    self._tool_result(s, call.id, call.name, await self._execute(s, call, trace))

            if s.pending:   # stop and hand control to the human; LLM resumes after the decision
                p = s.pending.public(s.student_no)
                lead = (resp.text.strip() + "\n\n") if resp.text.strip() else ""
                return AgentTurn(
                    lead + f"Please confirm: pay {p['amount']:,.2f} {p['currency']} toward {p['term']} "
                           f"(balance {p['balanceBefore']:,.2f} → {p['balanceAfter']:,.2f} {p['currency']}). "
                           "Nothing is charged until you press Confirm.",
                    trace, p)
        return AgentTurn("I could not finish this request within the allowed number of steps.", trace)

    # ------------------------------------------------------------ tool execution
    async def _execute(self, s: ChatSession, call: ToolCall, trace: list[dict]) -> dict:
        args = dict(call.arguments)
        t0 = time.perf_counter()
        if call.name in self._mcp_tool_names:
            result = await self.mcp.call(call.name, args)
            self._trace(trace, "MCP", call.name, input=args, ms=_ms(t0), output={
                "document_id": result.get("document_id"),
                "matches": [{"section": r["section"], "heading": r["heading"], "score": r["score"]}
                            for r in result.get("matches", [])]} if "matches" in result else result)
            return result

        if call.name == "get_tuition_balance":
            if "studentNo" in args or "student_no" in args:
                self._trace(trace, "GUARD", call.name, status="ignored",
                            output="LLM-supplied student number ignored; session identity used")
            term = args.get("term") or None
            if term and not TERM_RE.match(term):
                return {"error": "INVALID_TERM", "message": "Term must look like 2026-FALL"}
            status, body = await self.gateway.query_tuition(s.token, s.student_no, term)
            self._trace(trace, "REST via Gateway", call.name, input={"term": term}, output=body,
                        status=str(status), ms=_ms(t0),
                        endpoint=f"GET /api/v1/banking/tuition/{s.student_no}" + (f"?term={term}" if term else ""))
            return body if status == 200 else {"httpStatus": status, **body}

        self._trace(trace, "GUARD", call.name, status="rejected", output="unknown tool")
        return {"error": "UNKNOWN_TOOL", "message": f"No tool named {call.name}"}

    async def _prepare_payment(self, s: ChatSession, call: ToolCall, trace: list[dict]) -> dict | None:
        term = str(call.arguments.get("term", ""))
        try:
            amount = Decimal(str(call.arguments.get("amount")))
        except InvalidOperation:
            amount = Decimal(-1)
        if not TERM_RE.match(term):
            return {"error": "INVALID_TERM", "message": "Ask the student which term (e.g. 2026-FALL)."}
        if amount <= 0 or amount.as_tuple().exponent < -2:
            return {"error": "INVALID_AMOUNT", "message": "Amount must be positive with at most 2 decimals."}

        t0 = time.perf_counter()   # pre-check so the student never confirms an impossible payment
        status, body = await self.gateway.query_tuition(s.token, s.student_no, term)
        self._trace(trace, "REST via Gateway", "get_tuition_balance (pre-check)", input={"term": term},
                    output=body, status=str(status), ms=_ms(t0),
                    endpoint=f"GET /api/v1/banking/tuition/{s.student_no}?term={term}")
        if status != 200:
            return {"httpStatus": status, **body}
        balance = Decimal(str(body["balance"]))
        if amount > balance:
            return {"error": "AMOUNT_EXCEEDS_BALANCE", "outstandingBalance": float(balance),
                    "currency": body["currency"], "message": "Payment not prepared."}

        s.pending = PendingPayment(
            action_id=uuid.uuid4().hex[:12], tool_call_id=call.id, term=term, amount=float(amount),
            currency=body["currency"], balance_before=float(balance),
            transaction_id=f"AI-{uuid.uuid4().hex[:20].upper()}",
            expires_at=datetime.now(timezone.utc) + CONFIRMATION_TTL)
        self._trace(trace, "CONFIRMATION", "pay_tuition", status="awaiting-user",
                    input={"term": term, "amount": float(amount)}, output=s.pending.public(s.student_no))
        return None

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _tool_result(s: ChatSession, call_id: str, name: str, payload: Any) -> None:
        content = json.dumps(payload, ensure_ascii=False, default=str)
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + "…(truncated)"
        s.history.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": content})

    @staticmethod
    def _trace(trace: list[dict], type_: str, name: str, **kw) -> None:
        trace.append(asdict(TraceEvent(step=len(trace) + 1, type=type_, name=name, **kw)))


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)
