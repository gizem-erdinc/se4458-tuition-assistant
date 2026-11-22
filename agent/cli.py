"""Terminal client for the agent, plus the scripted demo of the required interactions.

    python -m agent.cli                         # interactive chat
    python -m agent.cli --demo                  # run the 4 demo conversations, save trace
    python -m agent.cli --demo --auto-approve   # same, approve the payment without prompting

Requires the gateway (8000) and the tuition API (8001) to be running, and an
LLM configured through environment variables (see .env.example).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from agent.backends import PolicyMcpClient, TuitionGatewayClient
from agent.llm import llm_from_env
from agent.orchestrator import AgentTurn, ChatSession, TuitionAgent

ROOT = Path(__file__).resolve().parent.parent

DEMO = [
    ("MCP only", "When is the payment deadline?"),
    ("REST API only", "What is my balance?"),
    ("MCP + REST API combined", "Can I pay my outstanding balance in installments?"),
    ("Payment (confirmation required)", "Pay part of my balance: 1000 USD for 2027-SPRING."),
]


def _short(v, n=140) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def print_turn(turn: AgentTurn) -> None:
    for ev in turn.trace:
        head = f"  [{ev['step']:>2}] {ev['type']:<17} {ev['name']}"
        if ev.get("endpoint"):
            head += f"  {ev['endpoint']} -> {ev['status']}"
        elif ev["status"] != "ok":
            head += f"  ({ev['status']})"
        print(head + (f"  {ev['ms']} ms" if ev.get("ms") else ""))
        if ev.get("input") is not None:
            print(f"        input : {_short(ev['input'])}")
        print(f"        output: {_short(ev['output'])}")
    print(f"\nASSISTANT: {turn.reply}\n")


async def ask_confirmation(agent: TuitionAgent, s: ChatSession, turn: AgentTurn, auto: bool,
                           log: list | None = None) -> AgentTurn:
    while turn.pending_action:
        p = turn.pending_action
        if auto:
            approve = True
            print(f"  >>> confirmation requested for {p['amount']} {p['currency']} — auto-approved (--auto-approve)")
        else:
            approve = input(f"  >>> Confirm payment of {p['amount']:.2f} {p['currency']} for {p['term']}? [y/N] ") \
                .strip().lower() in {"y", "yes", "e", "evet"}
        turn = await agent.resolve_confirmation(s, p["actionId"], approve)
        print_turn(turn)
        if log is not None:
            log.append({"user_action": "approve" if approve else "reject", "reply": turn.reply, "trace": turn.trace})
    return turn


async def run(args) -> None:
    mcp = PolicyMcpClient()
    await mcp.start()
    gw = TuitionGatewayClient()
    agent = TuitionAgent(llm_from_env(), mcp, gw)
    try:
        s = await agent.login(args.student, args.password)
        print(f"Logged in as {s.student_no}. Model: {agent.llm.model}. "
              f"Tools: {[t.name for t in agent.tool_specs()]}\n")
        if args.demo:
            record = {"started": datetime.now().isoformat(timespec="seconds"), "model": agent.llm.model,
                      "student": s.student_no, "scenarios": []}
            for title, q in DEMO:
                print("=" * 100 + f"\n{title}\nSTUDENT: {q}\n")
                turn = await agent.chat(s, q)
                print_turn(turn)
                entry = {"scenario": title, "question": q, "reply": turn.reply, "trace": turn.trace,
                         "pending_action": turn.pending_action, "follow_up": []}
                await ask_confirmation(agent, s, turn, args.auto_approve, entry["follow_up"])
                record["scenarios"].append(entry)
            events = [e for sc in record["scenarios"]
                      for e in sc["trace"] + [x for f in sc["follow_up"] for x in f["trace"]]]
            usage = [e["output"].get("usage", {}) for e in events
                     if e["type"] == "LLM" and isinstance(e["output"], dict)]
            record["token_usage"] = {"llm_calls": len(usage),
                                     "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
                                     "output_tokens": sum(u.get("output_tokens", 0) for u in usage)}
            print(f"LLM usage for the whole demo: {record['token_usage']}")
            out = ROOT / "docs" / "traces" / f"demo_{datetime.now():%Y%m%d_%H%M%S}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"Trace saved to {out.relative_to(ROOT)}")
        else:
            while True:
                try:
                    q = input("YOU: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if q in {"exit", "quit"}:
                    break
                if q:
                    turn = await agent.chat(s, q)
                    print_turn(turn)
                    await ask_confirmation(agent, s, turn, False)
    finally:
        await gw.close()
        await mcp.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student", default="20210001")
    ap.add_argument("--password", default="student123")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--auto-approve", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
