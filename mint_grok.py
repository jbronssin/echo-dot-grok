#!/usr/bin/env python3
"""Mint a Grok Bot voice call for the Dot. Loopback only.

Runs in a Mac GUI session, where the keychain item
"Grok Bot Safe Storage" can be read. Prints no secrets.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 19773
SECRETS = os.path.expanduser("~/Library/Application Support/Grok Bot/sand-secrets.json")
TOKEN_PATH = os.path.expanduser("~/.config/dot-voice/mint.token")
MINT_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/MintSandVoiceCallSecret"
HARNESS_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/VoiceCallHarnessSession"
TOOL_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/VoiceCallHarnessTool"
LIST_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/ListGrokBotAgents"
ME_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetMe"
VOICE_ID = re.compile(r"^[a-z0-9]{1,32}$")
# Server loads this bot's instructions and tools. The spoken prompt stays empty of the name.
AGENT_ID = os.environ.get("GROK_AGENT_ID", "").strip()
AGENT_NAME = os.environ.get("GROK_AGENT_NAME", "").strip()


def require_agent():
    if not AGENT_ID or not AGENT_NAME:
        raise RuntimeError("agent")


CALL_ID = re.compile(
    r"^call-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def load_token_file():
    return open(TOKEN_PATH).read().strip()


def safe_storage_password():
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", "Grok Bot Safe Storage", "-w"],
        capture_output=True,
        timeout=8,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("keychain")
    return proc.stdout.strip()


def decrypt(blob_b64, password):
    raw = base64.b64decode(blob_b64)
    if not raw.startswith(b"v10"):
        raise RuntimeError("cipher")
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", (b" " * 16).hex()],
        input=raw[3:],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout.startswith(b"eyJ"):
        raise RuntimeError("decrypt")
    return proc.stdout.decode().strip()


def access_token():
    data = json.load(open(SECRETS))
    accounts = json.loads(data["cursor-accounts"])
    entry = accounts["accounts"][accounts["active"]]
    return decrypt(entry["cursor-access-token"], safe_storage_password())


def post_json(url, payload, token, timeout=25):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError("upstream " + str(exc.code)) from None
    if not isinstance(body, dict):
        raise RuntimeError("upstream shape")
    return body


def account_name(token):
    # The app sends GetMe's "first last" as the harness userName. Omitting it
    # changes the plan. Empty name is omitted, same as a logged-out app.
    body = post_json(ME_URL, {}, token)
    parts = []
    for key in ("firstName", "lastName"):
        value = body.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value and "@" not in value and len(value) <= 80:
                parts.append(value)
    return " ".join(parts)


def agent_line(row):
    # One public line: uuid, display name, voice. Nothing else from the row.
    if not isinstance(row, dict):
        return ""
    agent_id = row.get("agentId")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return ""
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        name = row.get("agentName") if isinstance(row.get("agentName"), str) else ""
    name = " ".join(name.split())
    if "@" in name or len(name) > 80:
        name = ""
    voice = row.get("voiceId") or "-"
    if not isinstance(voice, str) or not voice.strip():
        voice = "-"
    return agent_id.strip() + "\t" + name + "\t" + voice.strip()


def list_agents():
    token = access_token()
    body = post_json(LIST_URL, {}, token)
    lines = []
    for row in body.get("agents") or []:
        line = agent_line(row)
        if line:
            lines.append(line)
    return lines


def agent_voice(token):
    require_agent()
    # Voice lives on the agent, not on the harness plan. null voiceId means
    # the app omits the field. null speed and language are the app defaults.
    body = post_json(LIST_URL, {}, token)
    for row in body.get("agents") or []:
        if not isinstance(row, dict) or row.get("agentId") != AGENT_ID:
            continue
        voice_id = row.get("voiceId")
        if voice_id is not None and (not isinstance(voice_id, str) or not VOICE_ID.match(voice_id)):
            raise RuntimeError("voice")
        speed = row.get("voiceSpeed")
        if speed is None:
            speed = 1
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
            raise RuntimeError("voice")
        language = row.get("voiceLanguage") or "auto"
        if not isinstance(language, str) or not language.strip() or len(language) > 32:
            raise RuntimeError("voice")
        return voice_id, speed, language
    raise RuntimeError("voice")


def voice_plan(token):
    require_agent()
    payload = {"agentId": AGENT_ID, "agentName": AGENT_NAME}
    user_name = account_name(token)
    if user_name:
        payload["userName"] = user_name
    body = post_json(HARNESS_URL, payload, token)
    instructions = body.get("instructions")
    tools = body.get("tools")
    if not isinstance(instructions, str) or len(instructions) < 100:
        raise RuntimeError("harness plan")
    if not isinstance(tools, list) or not tools:
        raise RuntimeError("harness tools")
    clean = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise RuntimeError("harness tools")
        params = tool.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        clean.append({
            "type": tool.get("type") or "function",
            "name": tool["name"],
            "description": tool.get("description") or "",
            "parameters": params,
        })
    receipt = ""
    rec = body.get("receipt")
    if isinstance(rec, dict) and isinstance(rec.get("instructions"), str):
        receipt = rec["instructions"]
    return instructions, clean, receipt


def run_tool(token, payload):
    require_agent()
    if not isinstance(payload, dict):
        raise RuntimeError("tool shape")
    call_id = payload.get("callId") or ""
    name = payload.get("name") or ""
    incoming = payload.get("input")
    line = payload.get("line")
    if not CALL_ID.match(str(call_id)) or not isinstance(name, str) or not 1 <= len(name) <= 200:
        raise RuntimeError("tool shape")
    if not isinstance(incoming, dict) or not isinstance(line, dict):
        raise RuntimeError("tool shape")
    body = post_json(TOOL_URL, {
        "agentId": AGENT_ID,
        "callId": call_id,
        "name": name,
        "input": incoming,
        "line": {
            "theyJustTalked": bool(line.get("theyJustTalked")),
            "newsOwed": bool(line.get("newsOwed")),
            "receiptOwed": bool(line.get("receiptOwed")),
            "alreadySpokeThisTurn": bool(line.get("alreadySpokeThisTurn")),
        },
    }, token)
    # Connect JSON emits the full enum name, not the app's short "served".
    kind = str(body.get("kind") or "")
    output = body.get("output")
    if kind.endswith("_SERVED") and isinstance(output, dict):
        return output
    if kind.endswith("_UNKNOWN_TOOL"):
        return {"error": "unknown_tool"}
    if kind.endswith("_INVALID_INPUT"):
        return {"error": "invalid_input"}
    return {"error": "tool"}


def mint():
    # The voice lookup and the call secret do not need the harness text.
    # They run beside it so unmute is not four round trips in a row.
    from concurrent.futures import ThreadPoolExecutor

    token = access_token()
    with ThreadPoolExecutor(max_workers=2) as pool:
        voice_f = pool.submit(agent_voice, token)
        secret_f = pool.submit(post_json, MINT_URL, {}, token, 20)
        instructions, tools, receipt = voice_plan(token)
        voice_id, speed, language = voice_f.result()
        body = secret_f.result()
    if not body.get("clientSecret") or not str(body.get("websocketUrl", "")).startswith("wss://"):
        raise RuntimeError("mint shape")
    return {
        "clientSecret": body["clientSecret"],
        "websocketUrl": body["websocketUrl"],
        "model": body.get("model") or "",
        "expiresAtUnixSeconds": body.get("expiresAtUnixSeconds"),
        "instructions": instructions,
        "tools": tools,
        "voiceId": voice_id,
        "voiceSpeed": speed,
        "voiceLanguage": language,
        "receipt": receipt,
    }


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        expected = "Bearer " + load_token_file()
        got = self.headers.get("Authorization", "")
        return bool(got) and hmac.compare_digest(got, expected)

    def answer(self, payload):
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != "/mint":
            self.send_error(404)
            return
        if not self.authorized():
            self.send_error(401)
            return
        try:
            payload = mint()
        except Exception as exc:
            print("mint fail", type(exc).__name__, flush=True)
            self.send_error(500)
            return
        self.answer(payload)
        print("mint ok", len(payload.get("tools") or []), payload.get("voiceId") or "-", flush=True)

    def do_POST(self):
        if self.path != "/tool":
            self.send_error(404)
            return
        if not self.authorized():
            self.send_error(401)
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = 0
        if n < 2 or n > 65536:
            self.send_error(400)
            return
        try:
            incoming = json.loads(self.rfile.read(n).decode())
            payload = run_tool(access_token(), incoming)
        except Exception as exc:
            print("tool fail", type(exc).__name__, flush=True)
            self.send_error(500)
            return
        self.answer(payload)
        print("tool ok", flush=True)

    def log_message(self, fmt, *args):
        return


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("listening", PORT, flush=True)
    server.serve_forever()


def list_check():
    assert agent_line({"agentId": "abc", "name": "Ada", "voiceId": "cosmo"}) == "abc\tAda\tcosmo"
    assert agent_line({"agentId": "abc", "agentName": "Ada"}) == "abc\tAda\t-"
    assert agent_line({"id": 1, "name": "Ada"}) == ""
    assert agent_line({"agentId": "abc", "name": "a@b.example"}) == "abc\t\t-"
    assert "instructions" not in agent_line({"agentId": "abc", "name": "Ada", "instructions": "secret"})
    print("list-check ok")


if __name__ == "__main__":
    if "--list-agents" in sys.argv:
        for line in list_agents():
            print(line)
    elif "--self-check" in sys.argv:
        list_check()
    else:
        try:
            main()
        except KeyboardInterrupt:
            sys.exit(0)
