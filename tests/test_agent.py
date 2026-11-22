"""Agent orchestration tests.

The LLM is replaced by a scripted test double so the tests are deterministic
and run offline. Everything else is real: the MCP server runs as a subprocess
and is called over the MCP protocol, and REST calls go agent -> gateway ->
tuition API -> SQLite (in-process via ASGI transports).
"""
import json

import httpx
import pytest

from agent.backends import PolicyMcpClient, TuitionGatewayClient
from agent.llm import AnthropicLLM, LLMResponse, OpenAICompatibleLLM, ToolCall
from agent.orchestrator import TuitionAgent
from tuition_api import db, service


class ScriptedLLM:
    """Each script step receives the history and returns an LLMResponse."""
    model = "scripted-test-llm"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[dict] = []

    async def complete(self, system, messages, tools):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages],
                           "tools": [t.name for t in tools]})
        return self.steps.pop(0)(messages)


def tool(name, **args):
    return lambda _h: LLMResponse("", [ToolCall(f"call_{name}_{len(args)}", name, args)])


def tools(*calls):
    return lambda _h: LLMResponse("", [ToolCall(f"call_{i}", n, a) for i, (n, a) in enumerate(calls)])


def answer(text):
    return lambda _h: LLMResponse(text)


def last_tool_results(history):
    return [json.loads(m["content"]) for m in history if m["role"] == "tool"]


@pytest.fixture
async def stack(gateway_app):
    mcp = PolicyMcpClient()
    await mcp.start()
    gw = TuitionGatewayClient("http://gateway", transport=httpx.ASGITransport(app=gateway_app))
    yield mcp, gw
    await gw.close()
    await mcp.close()
    await gateway_app.state.limiter.close()


async def make(stack, *steps):
    mcp, gw = stack
    llm = ScriptedLLM(*steps)
    agent = TuitionAgent(llm, mcp, gw, today="2026-11-22")
    session = await agent.login("20210001", "student123")
    return agent, session, llm


def balance(term="2027-SPRING"):
    with db.connect() as c:
        return service.query_tuition(c, "20210001", term)["balance"]


@pytest.mark.anyio
async def test_tools_offered_to_llm_include_discovered_mcp_tool(stack):
    agent, s, llm = await make(stack, answer("hi"))
    await agent.chat(s, "hello")
    assert llm.calls[0]["tools"] == ["search_tuition_policies", "get_tuition_balance", "pay_tuition"]
    assert "20210001" in llm.calls[0]["system"]


@pytest.mark.anyio
async def test_mcp_only(stack):
    agent, s, llm = await make(stack, tool("search_tuition_policies", query="fall payment deadline"),
                               answer("Fall tuition is due September 30, 2026 (Policy §2.1)."))
    turn = await agent.chat(s, "When is the payment deadline?")
    kinds = [e["type"] for e in turn.trace]
    assert kinds == ["LLM", "MCP", "LLM"]
    mcp_result = last_tool_results(llm.calls[1]["messages"])[0]
    assert mcp_result["document_id"] == "UNI-TUITION-2026-01"
    hit = next(m for m in mcp_result["matches"] if m["section"] == "2.1")
    assert "September 30, 2026" in hit["text"]


@pytest.mark.anyio
async def test_rest_only_and_identity_comes_from_session(stack):
    # the LLM tries to smuggle in another student's number -> ignored
    agent, s, llm = await make(stack, tool("get_tuition_balance", studentNo="20210002"),
                               answer("Your balance is 3,000 USD."))
    turn = await agent.chat(s, "What is my balance?")
    assert [e["type"] for e in turn.trace] == ["LLM", "GUARD", "REST via Gateway", "LLM"]
    rest = turn.trace[2]
    assert rest["endpoint"] == "GET /api/v1/banking/tuition/20210001" and rest["status"] == "200"
    assert last_tool_results(llm.calls[1]["messages"])[0]["balance"] == 3000.0


@pytest.mark.anyio
async def test_combined_mcp_and_rest(stack):
    agent, s, llm = await make(
        stack,
        tools(("get_tuition_balance", {"term": "2027-SPRING"}),
              ("search_tuition_policies", {"query": "installment eligibility number of installments"})),
        answer("Balance 3,000 USD; up to three installments (Policy §3.2)."))
    turn = await agent.chat(s, "Can I pay my outstanding balance in installments?")
    assert [e["type"] for e in turn.trace] == ["LLM", "REST via Gateway", "MCP", "LLM"]
    rest_result, mcp_result = last_tool_results(llm.calls[1]["messages"])
    assert rest_result["balance"] == 3000.0
    assert {"3.1", "3.2"} <= {r["section"] for r in mcp_result["matches"]}


@pytest.mark.anyio
async def test_payment_requires_explicit_confirmation(stack):
    agent, s, llm = await make(stack, tool("pay_tuition", term="2027-SPRING", amount=1000),
                               answer("Done: 1,000 USD paid, 2,000 USD remaining."))
    turn = await agent.chat(s, "Pay 1000 of my balance")
    assert turn.pending_action and turn.pending_action["amount"] == 1000.0
    assert turn.pending_action["balanceAfter"] == 2000.0
    assert balance() == 3000.0                                    # nothing charged yet
    assert all(e.get("endpoint") != "POST /api/v1/banking/payments" for e in turn.trace)
    assert len(llm.calls) == 1                                    # LLM paused while waiting for the human

    done = await agent.resolve_confirmation(s, turn.pending_action["actionId"], approve=True)
    pay = next(e for e in done.trace if e.get("endpoint") == "POST /api/v1/banking/payments")
    assert pay["status"] == "201" and pay["output"]["remainingBalance"] == 2000.0
    assert balance() == 2000.0
    again = await agent.resolve_confirmation(s, turn.pending_action["actionId"], approve=True)
    assert "no pending payment" in again.reply and balance() == 2000.0   # single use


@pytest.mark.anyio
async def test_payment_rejected_by_user_is_not_charged(stack):
    agent, s, llm = await make(stack, tool("pay_tuition", term="2027-SPRING", amount=500), answer("Cancelled."))
    turn = await agent.chat(s, "pay 500")
    await agent.resolve_confirmation(s, turn.pending_action["actionId"], approve=False)
    assert balance() == 3000.0
    assert last_tool_results(llm.calls[1]["messages"])[-1]["status"] == "CANCELLED_BY_STUDENT"


@pytest.mark.anyio
async def test_unconfirmed_payment_is_abandoned_when_user_moves_on(stack):
    agent, s, llm = await make(stack, tool("pay_tuition", term="2027-SPRING", amount=500), answer("ok"))
    await agent.chat(s, "pay 500")
    await agent.chat(s, "actually, what is the late fee?")
    assert balance() == 3000.0 and s.pending is None
    assert last_tool_results(llm.calls[1]["messages"])[-1]["status"] == "NOT_CONFIRMED"


@pytest.mark.anyio
async def test_overpayment_is_refused_before_confirmation(stack):
    agent, s, llm = await make(stack, tool("pay_tuition", term="2027-SPRING", amount=5000),
                               answer("You can pay at most 3,000 USD."))
    turn = await agent.chat(s, "pay 5000")
    assert turn.pending_action is None
    assert last_tool_results(llm.calls[1]["messages"])[-1]["error"] == "AMOUNT_EXCEEDS_BALANCE"


def test_provider_message_conversion():
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "", "tool_calls": [ToolCall("a", "t1", {"x": 1}),
                                                                   ToolCall("b", "t2", {})]},
               {"role": "tool", "tool_call_id": "a", "name": "t1", "content": "{}"},
               {"role": "tool", "tool_call_id": "b", "name": "t2", "content": "{}"}]
    anth = AnthropicLLM._convert(history)
    assert [m["role"] for m in anth] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in anth[2]["content"]] == ["a", "b"]       # results merged in one turn
    oai = OpenAICompatibleLLM._convert("sys", history)
    assert [m["role"] for m in oai] == ["system", "user", "assistant", "tool", "tool"]
    assert oai[2]["tool_calls"][0]["function"] == {"name": "t1", "arguments": '{"x": 1}'}


@pytest.mark.anyio
async def test_empty_optional_term_means_overall_balance(stack):
    """Regression: some models send term="" for an optional field (seen with Groq gpt-oss)."""
    agent, s, llm = await make(stack, tool("get_tuition_balance", term=""), answer("3,000 USD"))
    turn = await agent.chat(s, "What is my balance?")
    rest = next(e for e in turn.trace if e["type"] == "REST via Gateway")
    assert rest["endpoint"] == "GET /api/v1/banking/tuition/20210001" and rest["status"] == "200"
    assert last_tool_results(llm.calls[1]["messages"])[0]["scope"] == "OVERALL"


def test_rest_tool_schemas_have_no_provider_enforced_patterns():
    from agent.orchestrator import REST_TOOLS
    for t in REST_TOOLS:
        for prop in t.parameters["properties"].values():
            assert "pattern" not in prop and "exclusiveMinimum" not in prop
