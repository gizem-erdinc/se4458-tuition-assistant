"""Chat UI + JSON API for the AI agent.

    uvicorn agent.web:app --port 8080      ->  http://127.0.0.1:8080
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agent.backends import GatewayError, PolicyMcpClient, TuitionGatewayClient
from agent.llm import llm_from_env
from agent.orchestrator import ChatSession, TuitionAgent

STATIC = Path(__file__).resolve().parent / "static"
SESSIONS: dict[str, ChatSession] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp = PolicyMcpClient()
    await mcp.start()
    gateway = TuitionGatewayClient()
    app.state.agent = TuitionAgent(llm_from_env(), mcp, gateway)
    yield
    await gateway.close()
    await mcp.close()


app = FastAPI(title="University Tuition Assistant", lifespan=lifespan)


class LoginBody(BaseModel):
    studentNo: str = Field(pattern=r"^\d{8}$")
    password: str


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ConfirmBody(BaseModel):
    actionId: str
    approve: bool


def _session(sid: str | None) -> ChatSession:
    s = SESSIONS.get(sid or "")
    if s is None:
        raise HTTPException(401, "Please log in")
    return s


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/tools")
async def tools():
    return [{"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in app.state.agent.tool_specs()]


@app.post("/api/login")
async def login(body: LoginBody):
    try:
        s = await app.state.agent.login(body.studentNo, body.password)
    except GatewayError as e:
        raise HTTPException(401, e.body.get("error", {}).get("message", "Login failed"))
    SESSIONS[s.session_id] = s
    return {"sessionId": s.session_id, "studentNo": s.student_no, "model": app.state.agent.llm.model}


@app.post("/api/chat")
async def chat(body: ChatBody, x_session_id: str | None = Header(None)):
    turn = await app.state.agent.chat(_session(x_session_id), body.message)
    return {"reply": turn.reply, "trace": turn.trace, "pendingAction": turn.pending_action}


@app.post("/api/confirm")
async def confirm(body: ConfirmBody, x_session_id: str | None = Header(None)):
    turn = await app.state.agent.resolve_confirmation(_session(x_session_id), body.actionId, body.approve)
    return {"reply": turn.reply, "trace": turn.trace, "pendingAction": turn.pending_action}
