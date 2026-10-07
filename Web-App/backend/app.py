"""Simulated Alexa+ host.

Browser (voice + cards)  ->  this FastAPI app  ->  your MCP server (Streamable HTTP)

* POST /api/transcribe    Speech to text with Groq whisper-large-v3-turbo (raw audio in the body)
* POST /api/chat          gpt-oss-120b on Ollama (via LangChain) picks MCP tools; streams events (SSE)
* POST /api/tool/{name}   Call one MCP tool directly (planner form, Approve button, ...)
* GET  /api/health        Checks the MCP connection and lists the tools it exposes
* GET  /                  Serves the frontend

.env:
    GROQ_API_KEY       required (speech to text)
    OLLAMA_API_KEY     required for the hosted API at https://ollama.com
    OLLAMA_BASE_URL    default https://ollama.com   (use http://localhost:11434 for a local Ollama)
    OLLAMA_MODEL       default gpt-oss:120b         (hosted API: no ":cloud" suffix)
    MCP_URL            default http://localhost:8005/mcp
    STT_MODEL          default whisper-large-v3-turbo
    STT_LANGUAGE       default en (empty = auto-detect)
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import groq
import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from groq import AsyncGroq
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage, HumanMessage
from langchain_ollama import ChatOllama
from mcp import ClientSession
from ollama import ResponseError as OllamaResponseError
from pydantic import BaseModel

try:  # name differs between mcp SDK versions
    from mcp.client.streamable_http import streamablehttp_client as http_client
except ImportError:  # pragma: no cover
    from mcp.client.streamable_http import streamable_http_client as http_client

load_dotenv()

MCP_URL = os.getenv("MCP_URL", "http://localhost:8005/mcp")

# ----------------------------------------------------------------------- speech to text (Groq)
# Whisper is a speech model, not a chat model, so it can't go through ChatGroq.
# It uses Groq's audio transcription endpoint through the groq SDK (installed with langchain-groq).
groq_api_key = os.getenv("GROQ_API_KEY")
if not groq_api_key:
    raise ValueError("Missing GROQ_API_KEY in .env file")

groq_client = AsyncGroq(api_key=groq_api_key, base_url=os.getenv("GROQ_BASE_URL") or None)
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en")
# Whisper spells domain words better when it is primed with them.
STT_PROMPT = "Meal planning: calories, protein, carbs, fat, macros, pantry, inventory, groceries."

# ----------------------------------------------------------------------- reasoning model (Ollama)
ollama_bearer_token = os.getenv("OLLAMA_API_KEY", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")
MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:120b")

LLM_GPT = ChatOllama(
    model=MODEL,
    base_url=OLLAMA_BASE_URL,
    temperature=0.3,
    # the hosted API needs the key; a local Ollama doesn't
    client_kwargs={"headers": {"Authorization": "Bearer " + ollama_bearer_token}}
    if ollama_bearer_token
    else {},
)

MAX_STEPS = 6
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"

# The four tools your server exposes. Direct calls from the UI are limited to these.
ALLOWED_TOOLS = {"plan_meal", "manage_inventory", "shop_for_meal", "nutrition_status"}

SYSTEM = """You are a kitchen voice assistant on a smart display, simulating Alexa+. \
You help the user plan meals that hit calorie and macro targets using what they already have in their pantry.

How to answer:
- Speak naturally in one to three short sentences. No markdown, no bullet lists, no emoji. \
Details appear on screen as cards, so never read long lists aloud.
- Use the tools for anything about meals, pantry, shopping, or nutrition. Never guess nutrition numbers.
- Tools return a status and a spoken_summary. Treat status as authoritative. Only call a meal verified \
when its status is verified. If a result needs clarification, ask the user that question, then call \
plan_meal again with the same session_id and their answer.
- Tool Call Rules:
  * For `manage_inventory`, `free_text` MUST be a plain natural text string (e.g., '1000g of chicken breast'). Never pass a raw dictionary or list object.
  * For `plan_meal`, always pass a valid `target` dict containing positive numbers for calories or macros (e.g. `{"calories": 2000, "protein_g": 120}`).
- If the user gives no target for meal planning, ask for calories before planning.
- Shopping: prepare a cart with shop_for_meal using the plan_id. You cannot complete a purchase. \
After preparing a cart, tell the user to review it on screen and tap Approve to order.
- After a plan, offer exactly one useful next step."""

app = FastAPI(title="Alexa+ simulation host")

# conversation history per browser session (LangChain messages; in memory is fine for a demo)
SESSIONS: dict[str, list[BaseMessage]] = {}


# --------------------------------------------------------------------------- MCP


@asynccontextmanager
async def mcp_session():
    """Open a short-lived MCP session. The server is stateless, so this is cheap."""
    async with http_client(MCP_URL) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def parse_result(res: Any) -> dict[str, Any]:
    """Turn an MCP CallToolResult into {status, spoken_summary, resourceUri, data}."""
    structured = getattr(res, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured

    text = ""
    for block in getattr(res, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    if not getattr(res, "isError", False):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "status": "error" if getattr(res, "isError", False) else "unknown",
        "spoken_summary": text or "The tool returned no result.",
        "data": {},
    }


def for_llm(parsed: dict[str, Any]) -> str:
    slim = {k: v for k, v in parsed.items() if k != "resourceUri"}
    return json.dumps(slim, default=str)[:12000]


def sanitize_model_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Sanitize and normalize tool calls generated by the LLM before calling MCP."""
    args = dict(args)

    # 1. Procurement safety
    if name == "shop_for_meal":
        args.pop("approval_token", None)
        args["confirm"] = False
        args["user_approved"] = False

    # 2. Fix manage_inventory argument type mismatches (dict/list -> string)
    elif name == "manage_inventory":
        free_text = args.get("free_text")
        if isinstance(free_text, (dict, list)):
            if args.get("action") in {"add", "update"} and args.get("confirm"):
                items = free_text if isinstance(free_text, list) else [free_text]
                args["free_text"] = json.dumps(items)
            elif isinstance(free_text, dict):
                item_name = free_text.get("name", "")
                qty = free_text.get("quantity", free_text.get("available_grams", ""))
                unit = free_text.get("unit", "g")
                args["free_text"] = f"{qty}{unit} of {item_name}" if (item_name and qty) else json.dumps(free_text)
            else:
                args["free_text"] = json.dumps(free_text)

    # 3. Fix plan_meal target validation errors
    elif name == "plan_meal":
        target = args.get("target")
        if not isinstance(target, dict) or not any(
            isinstance(v, (int, float)) and v > 0 for v in target.values()
        ):
            # Inject a standard daily default target if model sends empty target {} or 0s
            args["target"] = {"calories": 2000, "protein_g": 130}

    return args


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


# --------------------------------------------------------------------------- LangChain helpers


def message_text(msg: AIMessage) -> str:
    """AIMessage.content can be a string or a list of content parts."""
    if isinstance(msg.content, str):
        return msg.content.strip()
    parts = []
    for part in msg.content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return " ".join(parts).strip()


def root_message(exc: BaseException) -> str:
    """Errors raised inside the MCP task group arrive wrapped in an ExceptionGroup; unwrap them."""
    while getattr(exc, "exceptions", None):
        exc = exc.exceptions[0]
    return str(exc) or type(exc).__name__


async def ask_llm(llm: Any, history: list[BaseMessage]) -> AIMessage:
    """Call gpt-oss and turn connection/auth problems into messages the UI can show."""
    try:
        return await llm.ainvoke([SystemMessage(content=SYSTEM), *history])
    except OllamaResponseError as exc:
        if exc.status_code in (401, 403):
            raise RuntimeError("Ollama rejected the credentials. Check OLLAMA_API_KEY in .env.") from exc
        if exc.status_code == 404:
            raise RuntimeError(
                f"Ollama has no model named {MODEL}. For the hosted API use gpt-oss:120b "
                "(no :cloud suffix); for a local Ollama, `ollama pull` it first."
            ) from exc
        raise RuntimeError(f"Ollama error {exc.status_code}: {exc.error}") from exc
    except (httpx.ConnectError, ConnectionError) as exc:
        raise RuntimeError(f"Could not reach Ollama at {OLLAMA_BASE_URL}.") from exc


# --------------------------------------------------------------------------- API


class ChatIn(BaseModel):
    message: str
    session_id: str | None = None
    context: str | None = None  # what the user just did on screen (form, buttons)


@app.get("/api/health")
async def health():
    try:
        async with mcp_session() as s:
            listed = await s.list_tools()
        return {
            "ok": True,
            "tools": [t.name for t in listed.tools],
            "model": MODEL,
            "provider": "ollama",
            "stt": STT_MODEL,
        }
    except BaseException as exc:  # noqa: BLE001 - surface any connection problem
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return JSONResponse(
            {"ok": False, "error": f"Could not reach the MCP server at {MCP_URL}", "detail": repr(exc)},
            status_code=503,
        )


@app.post("/api/transcribe")
async def transcribe(request: Request):
    """Speech to text. POST the raw audio bytes (e.g. a MediaRecorder webm blob) as the body."""
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "No audio received.")

    ctype = request.headers.get("content-type", "audio/webm").split(";")[0].strip().lower()
    ext = {
        "audio/webm": "webm",
        "video/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mp4": "m4a",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }.get(ctype, "webm")

    options: dict[str, Any] = {
        "model": STT_MODEL,
        "prompt": STT_PROMPT,
        "response_format": "json",
        "temperature": 0,
    }
    if STT_LANGUAGE:
        options["language"] = STT_LANGUAGE

    try:
        result = await groq_client.audio.transcriptions.create(
            file=(f"speech.{ext}", audio, ctype), **options
        )
    except groq.APIStatusError as exc:
        raise HTTPException(502, f"Groq transcription failed ({exc.status_code}): {exc.message}") from exc
    except groq.APIError as exc:
        raise HTTPException(502, f"Could not reach Groq: {exc.message}") from exc
    return {"text": (result.text or "").strip()}


@app.post("/api/tool/{name}")
async def call_tool_direct(name: str, args: dict[str, Any] = Body(...)):
    if name not in ALLOWED_TOOLS:
        raise HTTPException(404, f"Unknown tool: {name}")
    try:
        async with mcp_session() as s:
            res = await s.call_tool(name, args)
        return parse_result(res)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"MCP call failed: {exc!r}") from exc


@app.post("/api/chat")
async def chat(body: ChatIn):
    sid = body.session_id or uuid.uuid4().hex
    history = SESSIONS.setdefault(sid, [])
    start_len = len(history)

    text = body.message.strip()
    if body.context:
        text = f"[Screen context: {body.context}]\n{text}"
    history.append(HumanMessage(content=text))

    async def stream():
        yield sse({"type": "session", "id": sid})
        try:
            async with mcp_session() as session:
                listed = await session.list_tools()
                # the tools come live from your MCP server, so they are bound per request
                llm = LLM_GPT.bind_tools(
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": t.description or "",
                                "parameters": t.inputSchema,
                            },
                        }
                        for t in listed.tools
                    ]
                )

                for _ in range(MAX_STEPS):
                    ai = await ask_llm(llm, history)
                    history.append(ai)

                    calls = ai.tool_calls or []
                    said = message_text(ai)
                    final = not calls
                    if said:
                        yield sse({"type": "text", "text": said, "final": final})
                    if final:
                        break

                    for call in calls:
                        name = call["name"]
                        call_id = call.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                        args = sanitize_model_call(name, dict(call.get("args") or {}))

                        yield sse({"type": "tool_start", "id": call_id, "name": name, "args": args})
                        try:
                            parsed = parse_result(await session.call_tool(name, args))
                        except Exception as exc:  # noqa: BLE001
                            parsed = {
                                "status": "error",
                                "spoken_summary": f"The {name} tool failed: {exc}",
                                "data": {},
                            }
                        yield sse(
                            {
                                "type": "tool_result",
                                "id": call_id,
                                "name": name,
                                "args": args,
                                "result": parsed,
                            }
                        )
                        history.append(
                            ToolMessage(content=for_llm(parsed), tool_call_id=call_id, name=name)
                        )
                else:
                    yield sse(
                        {
                            "type": "text",
                            "text": "That took more steps than I expected. Check the cards on screen for what I found.",
                            "final": True,
                        }
                    )
            yield sse({"type": "done"})
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            del history[start_len:]  # never leave a dangling tool call in the history
            yield sse({"type": "error", "message": root_message(exc)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Static frontend last, so /api/* wins.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")