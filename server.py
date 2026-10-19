"""Private agent that runs inside a Tinfoil Container.

The server keeps no state. The Mac client sends the whole conversation on every
turn and runs the file tools on the Mac. This server adds the system prompt,
calls a Tinfoil model (with Tinfoil's private web search), and returns either a
final answer or the file-tool calls the model wants. It never logs message text.
"""

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 8_000_000
MODEL = os.environ.get("MODEL", "kimi-k3")

SYSTEM_PROMPT = """You are a private assistant for one person, Sam. He asks you personal, financial, legal, and medical questions he would not put into a normal chatbot. Answer directly and specifically. Give real numbers, names, and next steps. Say when you are unsure. Recommend a professional only when the situation calls for one, and still answer the question.

You can use these tools:
- Web search, built in. Search when the answer depends on current facts, prices, laws, or news. Write search queries in generic terms. Never put Sam's name, account numbers, addresses, or other identifying details in a search query.
- list_files, read_file, search_files. These read files in folders Sam has shared from his Mac. Use them when he refers to his documents, notes, statements, or records. Paths are relative to a shared folder.

Cite web sources inline as markdown links. When you used a file, name it."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in Sam's shared folders on his Mac. Use an empty path for the top level.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Folder path relative to the shared folders. Empty for the top level."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text, markdown, CSV, JSON, or PDF file from Sam's shared folders. Long files are cut off.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path as shown by list_files."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search file names and text inside Sam's shared folders. Returns matching paths with short snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Words to look for."}},
                "required": ["query"],
            },
        },
    },
]

_client = None
_client_lock = threading.Lock()


def log(message: str) -> None:
    print(message, flush=True)


def get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from tinfoil import TinfoilAI

                _client = TinfoilAI(api_key=os.environ["TINFOIL_API_KEY"], timeout=900)
    return _client


def authorized(header: str) -> bool:
    want = os.environ["AGENT_TOKEN"].encode()
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    got = header[len(prefix):].encode()
    return len(got) == len(want) and hmac.compare_digest(got, want)


def clean_messages(raw) -> list:
    """Keep only the fields the chat API accepts, so the client cannot inject a system prompt."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("messages must be a non-empty list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each message must be an object")
        role = item.get("role")
        if role == "user":
            out.append({"role": "user", "content": str(item.get("content") or "")})
        elif role == "assistant":
            msg = {"role": "assistant", "content": item.get("content") or ""}
            if item.get("tool_calls"):
                msg["tool_calls"] = item["tool_calls"]
            out.append(msg)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("tool_call_id") or ""),
                    "content": str(item.get("content") or ""),
                }
            )
        else:
            raise ValueError("role must be user, assistant, or tool")
    return out


def run_turn(body: dict) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + clean_messages(body.get("messages"))
    extra = {}
    if body.get("web_search", True):
        extra = {
            "web_search_options": {},
            "pii_check_options": {},
            "prompt_injection_check_options": {},
        }
    kwargs = {"model": MODEL, "messages": messages, "extra_body": extra}
    if body.get("file_tools", True):
        kwargs["tools"] = TOOLS
        kwargs["tool_choice"] = "auto"
    response = get_client().chat.completions.create(**kwargs)
    message = response.choices[0].message
    if message.tool_calls:
        calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"},
            }
            for call in message.tool_calls
        ]
        return {
            "type": "tool_calls",
            "message": {"role": "assistant", "content": message.content or "", "tool_calls": calls},
        }
    citations = []
    for ann in getattr(message, "annotations", None) or []:
        cite = getattr(ann, "url_citation", None)
        if cite is not None and getattr(cite, "url", None):
            citations.append({"url": cite.url, "title": getattr(cite, "title", "") or cite.url})
    return {
        "type": "final",
        "message": {"role": "assistant", "content": message.content or ""},
        "citations": citations,
        "model": MODEL,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return  # never log request lines

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"ok": True})
        elif path == "/":
            self._send(200, {"service": "private-agent", "model": MODEL})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/chat":
            self._send(404, {"error": "not found"})
            return
        if not authorized(self.headers.get("Authorization", "")):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"error": "body is empty or too large"})
            return
        started = time.monotonic()
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            result = run_turn(body)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        except Exception as exc:
            # Type and status only. Error text from the model API can echo the prompt.
            status = getattr(exc, "status_code", "")
            log(f"chat status=502 error={type(exc).__name__} upstream={status} ms={_ms(started)}")
            self._send(502, {"error": f"model call failed ({type(exc).__name__} {status})".strip()})
            return
        log(f"chat status=200 type={result['type']} ms={_ms(started)}")
        self._send(200, result)

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def main() -> None:
    missing = [name for name in ("AGENT_TOKEN", "TINFOIL_API_KEY") if not os.environ.get(name)]
    if missing:
        log("missing required environment: " + ", ".join(missing))
        sys.exit(1)
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"listening on {port} model={MODEL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
