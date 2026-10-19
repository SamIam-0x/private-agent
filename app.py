"""Local chat page for the private agent. Runs on the Mac at http://127.0.0.1:8767.

- Talks to the Tinfoil Container through tinfoil.SecureClient, which checks the
  enclave's attestation against the public config repo before sending anything.
- Stores chat history encrypted (AES-256-GCM) on disk. The key lives in the
  macOS Keychain.
- Runs file tools here on the Mac, limited to the folders listed in settings.
"""

import base64
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import keyring
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("PRIVATE_AGENT_PORT", "8767"))
DATA_DIR = Path(os.environ.get("PRIVATE_AGENT_DATA", Path.home() / "Library" / "Application Support" / "private-agent"))
HISTORY_DIR = DATA_DIR / "history"
SETTINGS_FILE = DATA_DIR / "settings.json"
KEYCHAIN_SERVICE = "private-agent"

# Where the agent lives. Override with PRIVATE_AGENT_URL=http://127.0.0.1:8080 for local testing.
ENCLAVE = os.environ.get("PRIVATE_AGENT_ENCLAVE", "private-agent.personal-sam.containers.tinfoil.dev")
CONFIG_REPO = os.environ.get("PRIVATE_AGENT_REPO", "SamIam-0x/private-agent")
LOCAL_URL = os.environ.get("PRIVATE_AGENT_URL", "")

MAX_TOOL_ROUNDS = 8
MAX_READ_CHARS = 60_000
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log",
    ".html", ".htm", ".xml", ".rtf", ".py", ".js", ".ts", ".sql", ".ini", ".toml",
}


# ---------- secrets ----------

def keychain_get(name: str, create: bool = False) -> str:
    value = keyring.get_password(KEYCHAIN_SERVICE, name)
    if value is None and create:
        value = base64.b64encode(secrets.token_bytes(32)).decode()
        keyring.set_password(KEYCHAIN_SERVICE, name, value)
    return value or ""


def history_key() -> bytes:
    return base64.b64decode(keychain_get("history-key", create=True))


# ---------- encrypted history ----------

class History:
    def __init__(self) -> None:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(DATA_DIR, 0o700)
        os.chmod(HISTORY_DIR, 0o700)
        self.aead = AESGCM(history_key())
        self.lock = threading.Lock()

    def _path(self, chat_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", chat_id):
            raise ValueError("bad chat id")
        return HISTORY_DIR / f"{chat_id}.enc"

    def load(self, chat_id: str) -> dict:
        raw = self._path(chat_id).read_bytes()
        plain = self.aead.decrypt(raw[:12], raw[12:], chat_id.encode())
        return json.loads(plain)

    def save(self, chat: dict) -> None:
        nonce = secrets.token_bytes(12)
        data = self.aead.encrypt(nonce, json.dumps(chat).encode(), chat["id"].encode())
        path = self._path(chat["id"])
        tmp = path.with_suffix(".tmp")
        with self.lock:
            tmp.write_bytes(nonce + data)
            os.chmod(tmp, 0o600)
            tmp.replace(path)

    def delete(self, chat_id: str) -> None:
        self._path(chat_id).unlink(missing_ok=True)

    def list(self) -> list:
        out = []
        for path in HISTORY_DIR.glob("*.enc"):
            try:
                chat = self.load(path.stem)
            except Exception:
                continue
            out.append({"id": chat["id"], "title": chat.get("title", "Untitled"), "updated": chat.get("updated", 0)})
        return sorted(out, key=lambda c: c["updated"], reverse=True)


# ---------- settings ----------

def load_settings() -> dict:
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
    except Exception:
        settings = {}
    settings.setdefault("folders", [])
    settings.setdefault("web_search", True)
    return settings


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    os.chmod(SETTINGS_FILE, 0o600)


# ---------- file tools (run on the Mac) ----------

def shared_roots() -> dict:
    roots = {}
    for folder in load_settings()["folders"]:
        path = Path(folder).expanduser().resolve()
        if path.is_dir():
            name = path.name
            while name in roots:
                name += "_"
            roots[name] = path
    return roots


def resolve(rel: str) -> Path:
    rel = (rel or "").strip().strip("/")
    roots = shared_roots()
    if not rel:
        raise LookupError("top")
    top, _, rest = rel.partition("/")
    if top not in roots:
        raise PermissionError(f"'{top}' is not a shared folder. Shared folders: {', '.join(roots) or 'none'}")
    root = roots[top]
    target = (root / rest).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("path is outside the shared folders")
    return target


def display(path: Path) -> str:
    for name, root in shared_roots().items():
        if path == root or root in path.parents:
            return str(Path(name) / path.relative_to(root))
    return path.name


def tool_list_files(args: dict) -> str:
    try:
        target = resolve(args.get("path", ""))
    except LookupError:
        roots = shared_roots()
        return "\n".join(f"{name}/" for name in roots) or "No folders are shared. Sam can add some in Settings."
    if not target.is_dir():
        return "not a folder"
    lines = []
    for child in sorted(target.iterdir()):
        if child.name.startswith("."):
            continue
        lines.append(display(child) + ("/" if child.is_dir() else f"  ({child.stat().st_size} bytes)"))
        if len(lines) >= 300:
            lines.append("... more not shown")
            break
    return "\n".join(lines) or "(empty)"


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = []
        for number, page in enumerate(reader.pages, 1):
            parts.append(f"[page {number}]\n{page.extract_text() or ''}")
            if sum(len(p) for p in parts) > MAX_READ_CHARS:
                break
        return "\n".join(parts)
    return path.read_text(errors="replace")


def tool_read_file(args: dict) -> str:
    target = resolve(args.get("path", ""))
    if not target.is_file():
        return "file not found"
    if target.suffix.lower() not in TEXT_SUFFIXES | {".pdf"} and target.stat().st_size > 200_000:
        return "this file type is not supported"
    text = read_text(target)
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n\n[cut off at {MAX_READ_CHARS} characters of {len(text)}]"
    return text


def tool_search_files(args: dict) -> str:
    words = [w.lower() for w in str(args.get("query", "")).split() if w]
    if not words:
        return "empty query"
    hits, scanned = [], 0
    for root in shared_roots().values():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", ".venv", "__pycache__")]
            for name in filenames:
                if name.startswith("."):
                    continue
                path = Path(dirpath) / name
                scanned += 1
                if scanned > 20_000:
                    break
                if all(w in name.lower() for w in words):
                    hits.append(f"{display(path)}  (name match)")
                elif path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size < 2_000_000:
                    try:
                        text = path.read_text(errors="ignore")
                    except Exception:
                        continue
                    low = text.lower()
                    if all(w in low for w in words):
                        i = low.find(words[0])
                        snippet = text[max(0, i - 80): i + 120].replace("\n", " ")
                        hits.append(f"{display(path)}: ...{snippet}...")
                if len(hits) >= 30:
                    return "\n".join(hits)
    return "\n".join(hits) or "no matches"


TOOL_FUNCS = {"list_files": tool_list_files, "read_file": tool_read_file, "search_files": tool_search_files}


def run_tool(call: dict) -> str:
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"unknown tool {name}"
    try:
        return func(args)
    except Exception as exc:
        return f"error: {exc}"


# ---------- connection to the enclave ----------

class Agent:
    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()

    def token(self) -> str:
        token = keychain_get("agent-token")
        if not token:
            raise RuntimeError("No agent token in the Keychain. Run setup first (see README).")
        return token

    def client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from tinfoil import SecureClient

                    client = SecureClient(enclave=ENCLAVE, repo=CONFIG_REPO)
                    client.verify()  # raises if the enclave does not match the public config
                    self._client = client
        return self._client

    def verification(self) -> dict:
        if LOCAL_URL:
            return {"verified": False, "mode": "local test", "url": LOCAL_URL}
        doc = self.client().get_verification_document()
        return {
            "verified": True,
            "enclave": ENCLAVE,
            "repo": CONFIG_REPO,
            "release": getattr(doc, "release_tag", None),
            "code_fingerprint": getattr(doc, "code_fingerprint", None),
            "enclave_fingerprint": getattr(doc, "enclave_fingerprint", None),
        }

    def post_chat(self, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token()}"}
        if LOCAL_URL:
            import urllib.request

            req = urllib.request.Request(LOCAL_URL + "/chat", data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=900) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"agent returned {exc.code}: {exc.read().decode(errors='replace')}")
        try:
            resp = self.client().post(f"https://{ENCLAVE}/chat", headers=headers, body=body)
        except Exception:
            self._client = None  # re-verify on the next try (enclave may have restarted)
            raise
        if resp.status_code != 200:
            raise RuntimeError(f"agent returned {resp.status}: {resp.body.decode(errors='replace')[:300]}")
        return json.loads(resp.body)

    def ask(self, messages: list, web_search: bool) -> tuple:
        """Run the tool loop. Returns (new messages to append, citations)."""
        added, convo = [], list(messages)
        for _ in range(MAX_TOOL_ROUNDS):
            result = self.post_chat({"messages": convo, "web_search": web_search, "file_tools": True})
            msg = result["message"]
            added.append(msg)
            convo.append(msg)
            if result["type"] == "final":
                return added, result.get("citations", [])
            for call in msg["tool_calls"]:
                tool_msg = {"role": "tool", "tool_call_id": call["id"], "content": run_tool(call), "name": call["function"]["name"]}
                added.append(tool_msg)
                convo.append(tool_msg)
        added.append({"role": "assistant", "content": "I stopped after too many file lookups. Try a narrower question."})
        return added, []


# ---------- local web server ----------

HISTORY = None
AGENT = Agent()
SESSION = secrets.token_urlsafe(24)  # the page must send this back; blocks other sites and local apps


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        path = self.path.split("?", 1)[0]
        if path == "/":
            html = (HERE / "ui.html").read_text().replace("__SESSION__", SESSION)
            return self._send_raw(200, html.encode(), "text/html; charset=utf-8")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Session") != SESSION:
            return self._send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        route = self.path.split("?", 1)[0]
        try:
            if route == "/api/chats":
                return self._send(200, {"chats": HISTORY.list()})
            if route == "/api/chat":
                return self._send(200, HISTORY.load(body["id"]))
            if route == "/api/delete":
                HISTORY.delete(body["id"])
                return self._send(200, {"ok": True})
            if route == "/api/settings":
                settings = load_settings()
                if "folders" in body:
                    settings["folders"] = [f.strip() for f in body["folders"] if f.strip()]
                if "web_search" in body:
                    settings["web_search"] = bool(body["web_search"])
                save_settings(settings)
                return self._send(200, settings)
            if route == "/api/verify":
                return self._send(200, AGENT.verification())
            if route == "/api/send":
                return self._send(200, self._handle_send(body))
        except Exception as exc:
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, {"error": "not found"})

    def _handle_send(self, body: dict) -> dict:
        text = str(body.get("text", "")).strip()
        if not text:
            raise ValueError("empty message")
        if body.get("id"):
            chat = HISTORY.load(body["id"])
        else:
            chat = {"id": uuid.uuid4().hex, "title": text[:60], "created": time.time(), "messages": []}
        chat["messages"].append({"role": "user", "content": text})
        api_messages = [{k: v for k, v in m.items() if k not in ("name", "citations")} for m in chat["messages"]]
        added, citations = AGENT.ask(api_messages, load_settings()["web_search"])
        if citations:
            added[-1]["citations"] = citations
        chat["messages"].extend(added)
        chat["updated"] = time.time()
        HISTORY.save(chat)
        return chat

    def _send(self, status, payload):
        self._send_raw(status, json.dumps(payload).encode(), "application/json")

    def _send_raw(self, status, raw, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def setup_token() -> None:
    """Create the shared bearer token, store it in the Keychain, and print nothing secret."""
    if keychain_get("agent-token"):
        print("Agent token already in the Keychain.")
        return
    keyring.set_password(KEYCHAIN_SERVICE, "agent-token", secrets.token_urlsafe(32))
    print("Created agent token in the Keychain (service 'private-agent').")


def main() -> None:
    global HISTORY
    if len(sys.argv) > 1 and sys.argv[1] == "setup-token":
        return setup_token()
    if len(sys.argv) > 1 and sys.argv[1] == "print-token":  # used only to pipe into `tinfoil secret create`
        sys.stdout.write(keychain_get("agent-token"))
        return
    HISTORY = History()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Private agent running at {url}  (enclave: {LOCAL_URL or ENCLAVE})")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
