#!/usr/bin/env python3
"""Echo Dot handset. Unmute opens a live voice line.
Mute while a reply is playing cuts it and listens. Mute while
listening hangs up.

The call is minted by the Grok Bot app on a Mac (grok-voice-synapse).
That Mac's keychain stays there. This process only receives a short-lived
call secret over SSH and throws it away when the call ends.
"""

import array
import base64
import hashlib
import json
import math
import os
import queue
import select
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import uuid

# ponytail: fixed 6x. Room floor on this mic is about 10, and a voice that
# is not shouted into it stays under server VAD. Clip at int16.
# Upgrade path is AGC if close speech sounds blown out.
MIC_GAIN = 6
# Raw mic RMS. A full-scale tone at codec volume 115 came back well under
# this (amp 5000 measured mic rms 81). Two 100 ms chunks this loud mean
# someone is talking over the speaker, not the speaker hearing itself.
# ponytail: a far, quiet voice stays under the line and does not cut.
# Upgrade path is the HAL echo canceller.
BARGE_RMS = 1600
MIC_PORT = 19771
SPK_PORT = 19772
IN_RATE = 16000
API_RATE = 24000
OUT_RATE = 48000
DOT_IP = os.environ.get("DOT_IP", "").strip()
ADB = os.environ.get("ADB", "adb").strip() or "adb"
SERIAL = os.environ.get("DOT_SERIAL", "").strip() or (f"{DOT_IP}:5555" if DOT_IP else "")
ALLOW = tuple(
    part.strip() for part in os.environ.get("DOT_ADB_ALLOW", "").split(",") if part.strip()
)
MINT_SSH = os.environ.get("DOT_MINT_SSH", "").strip()
MINT_KEY = os.environ.get("DOT_MINT_KEY", "").strip()


def _ipv4(text):
    parts = text.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or part != str(int(part)) or int(part) > 255:
            return False
    return True


# ponytail: linear fill to 48 kHz. Repeating samples buzzed on this speaker.
# Upgrade path is ffmpeg aresample if speech still sounds harsh.


def upsample(pcm: bytes, factor: int) -> bytes:
    if factor < 1:
        factor = 1
    if len(pcm) < 2:
        return b""
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if factor == 1 or len(src) == 0:
        return src.tobytes()
    out = array.array("h")
    last = len(src) - 1
    for i, sample in enumerate(src):
        nxt = src[i + 1] if i < last else sample
        for k in range(factor):
            out.append(sample + (nxt - sample) * k // factor)
    return out.tobytes()


def up3(pcm: bytes) -> bytes:
    return upsample(pcm, 3)


def to_api(pcm: bytes) -> bytes:
    # ponytail: linear 16 kHz -> 24 kHz. A chunk boundary skips one sample of
    # interpolation. Upgrade path is ffmpeg aresample if the model mishears.
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not src:
        return b""
    out_n = len(src) * API_RATE // IN_RATE
    out = array.array("h")
    span = len(src) - 1
    for i in range(out_n):
        num = i * IN_RATE
        i0 = num // API_RATE
        if i0 >= span:
            out.append(src[-1])
            continue
        frac = num % API_RATE
        s0 = src[i0]
        s1 = src[i0 + 1]
        out.append(s0 + (s1 - s0) * frac // API_RATE)
    return out.tobytes()


def tone(freq: int, ms: int, amp: int = 2000) -> bytes:
    n = OUT_RATE * ms // 1000
    buf = array.array("h")
    for i in range(n):
        buf.append(int(amp * math.sin(2 * math.pi * freq * i / OUT_RATE)))
    return buf.tobytes()


# Playd keeps a 32768-byte track full of silence, so a click written
# now is still behind that audio. Hold the mic through it.
TRACK_LAG = 32768 / 2 / OUT_RATE


def handset(which: str) -> bytes:
    """Cradle click. A sine reads as an alarm on this speaker.

    Open is the lighter lift. Close is the heavier set-down. Partial
    tones sit in the band this cone can make. The first 2 ms is ramped
    so the cone does not pop.
    """
    if which == "open":
        ms = 70
        parts = (
            (0, 1800, 6000, 0.008),
            (0, 900, 5000, 0.018),
            (16, 2400, 4000, 0.005),
        )
    elif which == "close":
        ms = 130
        parts = (
            (0, 220, 14000, 0.04),
            (0, 440, 6000, 0.02),
            (0, 1500, 4500, 0.008),
            (40, 200, 6000, 0.02),
        )
    else:
        raise RuntimeError("handset")
    buf = [0.0] * (OUT_RATE * ms // 1000)
    for start_ms, freq, amp, tau in parts:
        start = OUT_RATE * start_ms // 1000
        n = int(OUT_RATE * tau * 6)
        for i in range(n):
            j = start + i
            if j >= len(buf):
                break
            t = i / OUT_RATE
            buf[j] += amp * math.sin(2 * math.pi * freq * t) * math.exp(-t / tau)
    attack = int(OUT_RATE * 0.002)
    for i in range(attack):
        buf[i] *= i / attack
    out = array.array("h")
    for sample in buf:
        value = int(sample)
        if value > 32767:
            value = 32767
        elif value < -32768:
            value = -32768
        out.append(value)
    return out.tobytes()


def session_update(plan: dict) -> dict:
    instructions = plan.get("instructions")
    tools = plan.get("tools")
    # No generic fallback. An empty plan is the call that answered "None."
    if not isinstance(instructions, str) or not instructions.strip():
        raise RuntimeError("harness plan")
    if not isinstance(tools, list):
        raise RuntimeError("harness tools")
    if "voice" not in plan:
        raise RuntimeError("harness voice")
    voice = plan.get("voice")
    if voice is not None and (not isinstance(voice, str) or not voice.strip()):
        raise RuntimeError("harness voice")
    speed = plan.get("speed", 1)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
        raise RuntimeError("harness voice")
    language = plan.get("language") or "auto"
    if not isinstance(language, str):
        raise RuntimeError("harness voice")
    transcription = {"model": "grok-transcribe"}
    if language != "auto":
        transcription["language_hint"] = language
    session = {
        "resumption": {"enabled": True},
        "instructions": instructions,
        "turn_detection": {"type": "server_vad"},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": API_RATE},
                "transport": "binary",
                "transcription": transcription,
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": API_RATE},
                "transport": "binary",
                "speed": speed,
            },
        },
        "tools": tools,
    }
    # The app omits voice when the agent has none. A hardcoded name was Eve.
    if voice is not None:
        session["voice"] = voice
    return {"type": "session.update", "session": session}


def adb(*args, timeout=20):
    cmd = [ADB, "-s", SERIAL, *args]
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def sh(script, timeout=20):
    quoted = script.replace("'", "'\\''")
    return adb("shell", f"su -c '{quoted}'", timeout=timeout)


def connect_adb():
    if os.sep in ADB and not os.path.exists(ADB):
        sys.exit(f"adb missing: {ADB}")
    try:
        subprocess.run([ADB, "connect", SERIAL], capture_output=True, timeout=15)
    except FileNotFoundError:
        sys.exit(f"adb missing: {ADB}")
    st = adb("get-state", timeout=10)
    if st.returncode != 0 or b"device" not in st.stdout:
        sys.exit(f"adb not connected to {SERIAL}")


def lock_adb_port():
    # ponytail: applied when the bridge starts, not at boot. A few seconds
    # after reboot the port is open to the LAN. A Magisk service.d script
    # would close that window.
    # Empty allow list must not install the DROP. That would lock the
    # bridge out of its own adb session.
    if not ALLOW:
        print("adb 5555 left open. set DOT_ADB_ALLOW to lock it.", flush=True)
        return
    for ip in ALLOW:
        if not _ipv4(ip):
            sys.exit("DOT_ADB_ALLOW")
    rules = []
    for ip in ALLOW:
        rules.append(
            f"iptables -C INPUT -p tcp --dport 5555 -s {ip} -j ACCEPT "
            f"|| iptables -I INPUT 1 -p tcp --dport 5555 -s {ip} -j ACCEPT"
        )
    rules.append(
        "iptables -C INPUT -p tcp --dport 5555 -j DROP "
        "|| iptables -A INPUT -p tcp --dport 5555 -j DROP"
    )
    sh("; ".join(rules))


def _kill_app(pid_name):
    # pid file only. Never kill every app_process: that is zygote.
    sh(
        f"if [ -f /data/local/tmp/{pid_name}.pid ]; then "
        f"kill $(cat /data/local/tmp/{pid_name}.pid) 2>/dev/null; "
        f"rm -f /data/local/tmp/{pid_name}.pid; fi"
    )


def _launch(cls):
    proc = subprocess.Popen(
        [ADB, "-s", SERIAL, "shell",
         f"su -c 'CLASSPATH=/data/local/tmp/dotd.dex app_process /data/local/tmp {cls}'"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    ready = False
    deadline = time.time() + 8
    buf = b""
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stdout.read() or b""
            sys.exit(cls + " exited: " + (buf + err).decode(errors="replace")[:400])
        readable, _, _ = select.select([proc.stdout], [], [], 0.4)
        if not readable:
            continue
        chunk = proc.stdout.read(1)
        if not chunk:
            continue
        buf += chunk
        if b"READY" in buf:
            ready = True
            break
    if not ready:
        proc.kill()
        sys.exit(cls + " did not say READY: " + buf.decode(errors="replace")[:400])
    return proc


def ensure_dotd():
    dex_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dotd.dex")
    if not os.path.exists(dex_local):
        sys.exit(f"missing {dex_local}")
    adb("push", dex_local, "/data/local/tmp/dotd.dex", timeout=30)
    sh("chmod 644 /data/local/tmp/dotd.dex")
    _kill_app("dotd")
    _kill_app("playd")
    time.sleep(0.3)
    # Speaker first, so the startup beep has a track that is already listening.
    play_proc = _launch("Playd")
    dot_proc = _launch("Dotd")
    # HD off plays the tone as chopped blasts. On is a steady tone.
    # Volume 100 is too quiet. 140 is loud enough to crackle. 115 is the
    # level a room mic recorded as a continuous 440 with no gaps.
    sh('tinymix "Audio_I2S0dl1_hd_Switch" On')
    sh('tinymix "PCM Playback Volume" 115 115')
    adb("forward", f"tcp:{MIC_PORT}", f"tcp:{MIC_PORT}")
    adb("forward", f"tcp:{SPK_PORT}", f"tcp:{SPK_PORT}")
    return play_proc, dot_proc


def open_socks():
    mic = socket.create_connection(("127.0.0.1", MIC_PORT), 5)
    spk = socket.create_connection(("127.0.0.1", SPK_PORT), 5)
    mic.setblocking(False)
    spk.settimeout(5)
    return mic, spk


# inflight, pending, owed, after_tool, spoke, nudged
GATE0 = (0, False, False, False, False, False)
# Per-response override. The session prompt comes back on the next turn.
# Docs: instructions here replace the system prompt for this create only.
SPEAK_AGAIN = (
    "This turn must be spoken audio, in the caller's language. "
    "Say the tool result. Do not call a tool."
)


def reply_gate(state, event):
    """state is (inflight, pending, owed, after_tool, spoke, nudged).

    send is "tool", "nudge", or False.

    One tool cluster gets one response.create. A create sent while a
    response is still open stays queued and plays the same sentence again.

    A tool result that arrives after create was sent, but before
    response.created, is owed. The follow-up create goes out when the
    open response finishes.

    That follow-up sometimes closes with no audio. One nudge asks for
    speech. A second silent close does not ask again.
    """
    inflight, pending, owed, after_tool, spoke, nudged = state
    send = False
    if event == "audio":
        spoke = True
    elif event == "created":
        inflight += 1
        pending = False
        spoke = False
    elif event == "done":
        inflight = max(0, inflight - 1)
        if inflight == 0 and owed and not pending:
            send = "tool"
            pending = True
            owed = False
            after_tool = True
            nudged = False
        elif inflight == 0 and after_tool and not spoke and not nudged:
            send = "nudge"
            pending = True
            nudged = True
            after_tool = False
        elif inflight == 0 and spoke:
            after_tool = False
            nudged = False
    elif event == "tool":
        if inflight or pending:
            owed = True
        else:
            send = "tool"
            pending = True
            owed = False
            after_tool = True
            nudged = False
    return (inflight, pending, owed, after_tool, spoke, nudged), send


def light_cmds(state):
    """LED commands for one ring state. Newest queued state wins.

    active-talking is the registered cyan breath. dialog-talk is on
    disk but not in the layer list, so ledctrl accepts it and the ring
    stays off. Idle also drops the purple setup pulse.
    """
    if state == "talk":
        return ("/system/bin/ledctrl --set_pattern=active-talking",)
    if state == "idle":
        return (
            "/system/bin/ledctrl --unset_pattern=active-talking",
            "/system/bin/ledctrl --unset_pattern=anim_OOBE_start_error",
        )
    raise RuntimeError("light")


def light_worker(q):
    last = None
    while True:
        state = q.get()
        while True:
            try:
                state = q.get_nowait()
            except queue.Empty:
                break
        failed = False
        for cmd in light_cmds(state):
            proc = sh(cmd, timeout=8)
            if proc.returncode != 0:
                failed = True
                print("light fail", state, proc.returncode, flush=True)
        if not failed and state != last:
            print("light", state, flush=True)
            last = state


def send_pcm(spk, pcm, watch):
    """Send speaker audio in small pieces. watch() True means mute: stop now.

    A blocking sendall holds the button until the queued reply fits in the
    socket, so the Dot keeps talking after the press.
    """
    view = memoryview(pcm)
    spk.settimeout(0.05)
    try:
        while len(view):
            if watch():
                return False
            try:
                n = spk.send(view[:4096])
            except socket.timeout:
                continue
            if not n:
                return False
            view = view[n:]
    finally:
        spk.settimeout(5)
    return True


class Ws:
    """Stdlib websocket client. Client frames are masked. One host only."""

    def __init__(self, url: str, header: str = "", protocol: str = ""):
        if not url.startswith("wss://"):
            raise ValueError(url)
        rest = url[len("wss://"):]
        host, path = rest.split("/", 1)
        path = "/" + path
        port = 443
        if ":" in host:
            host, port_s = host.split(":")
            port = int(port_s)
        raw = socket.create_connection((host, port), 15)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
        )
        if protocol:
            req += f"Sec-WebSocket-Protocol: {protocol}\r\n"
        if header:
            req += f"Authorization: {header}\r\n"
        req += "\r\n"
        self.sock.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ws handshake closed")
            data += chunk
        status = data.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise RuntimeError(status.decode(errors="replace")[:200])
        self.buf = data.split(b"\r\n\r\n", 1)[1]
        self.sock.setblocking(False)

    def send_text(self, text: str):
        self._send(1, text.encode())

    def send_bin(self, payload: bytes):
        self._send(2, payload)

    def _send(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.setblocking(True)
        self.sock.sendall(head + mask + masked)
        self.sock.setblocking(False)

    def recv(self, timeout=0.0):
        if timeout:
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r and len(self.buf) < 2:
                return None
        self.sock.setblocking(False)
        while len(self.buf) < 2:
            try:
                chunk = self.sock.recv(65536)
            except (BlockingIOError, ssl.SSLWantReadError):
                return None
            if not chunk:
                return None
            self.buf += chunk
        b0, b1 = self.buf[0], self.buf[1]
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        i = 2
        if n == 126:
            while len(self.buf) < 4:
                self.buf += self._read()
            n = struct.unpack("!H", self.buf[2:4])[0]
            i = 4
        elif n == 127:
            while len(self.buf) < 10:
                self.buf += self._read()
            n = struct.unpack("!Q", self.buf[2:10])[0]
            i = 10
        while len(self.buf) < i + n:
            self.buf += self._read()
        payload = self.buf[i:i + n]
        self.buf = self.buf[i + n:]
        if opcode == 8:
            return None
        if opcode == 9:
            self._send(10, payload)
            return self.recv(0)
        return opcode, payload

    def _read(self):
        self.sock.setblocking(True)
        self.sock.settimeout(10)
        chunk = self.sock.recv(65536)
        self.sock.setblocking(False)
        if not chunk:
            raise RuntimeError("ws closed")
        return chunk

    def close(self):
        try:
            self._send(8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def _mint_ssh(remote, timeout):
    return subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "IdentitiesOnly=yes", "-i", MINT_KEY, MINT_SSH, remote,
        ],
        capture_output=True,
        timeout=timeout,
    )


def fetch_mint():
    if not MINT_SSH or not MINT_KEY:
        raise RuntimeError("mint ssh")
    proc = _mint_ssh("mint", 40)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("mint ssh failed")
    try:
        data = json.loads(proc.stdout.decode())
    except json.JSONDecodeError:
        raise RuntimeError("mint bad json")
    secret = data.get("clientSecret") or ""
    url = data.get("websocketUrl") or ""
    model = data.get("model") or ""
    instructions = data.get("instructions")
    tools = data.get("tools")
    if not secret or any(ch.isspace() for ch in secret) or not url.startswith("wss://"):
        raise RuntimeError("mint missing fields")
    if not isinstance(instructions, str) or not instructions.strip() or not isinstance(tools, list):
        raise RuntimeError("mint missing plan")
    if "voiceId" not in data or "voiceSpeed" not in data or "voiceLanguage" not in data:
        raise RuntimeError("mint voice")
    voice = data.get("voiceId")
    if voice is not None and not isinstance(voice, str):
        raise RuntimeError("mint voice")
    speed = data.get("voiceSpeed")
    language = data.get("voiceLanguage") or "auto"
    receipt = data.get("receipt") or ""
    if not isinstance(receipt, str):
        receipt = ""
    if model and "model=" not in url:
        url += ("&" if "?" in url else "?") + "model=" + model
    return url, secret, {
        "instructions": instructions,
        "tools": tools,
        "voice": voice,
        "speed": speed,
        "language": language,
        "receipt": receipt,
    }


def fetch_tool(call_id, name, raw_args):
    # One SSH per tool. The audio loop does not wait on this.
    try:
        incoming = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        incoming = {}
    if not isinstance(incoming, dict):
        incoming = {}
    body = json.dumps({
        "callId": call_id,
        "name": name,
        "input": incoming,
        "line": {
            "theyJustTalked": True,
            "newsOwed": False,
            "receiptOwed": False,
            "alreadySpokeThisTurn": False,
        },
    }).encode()
    proc = _mint_ssh("tool " + base64.b64encode(body).decode(), 40)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("tool ssh failed")
    try:
        data = json.loads(proc.stdout.decode())
    except json.JSONDecodeError:
        raise RuntimeError("tool bad json")
    if not isinstance(data, dict):
        raise RuntimeError("tool shape")
    return json.dumps(data)


def check_ws():
    url, secret, plan = fetch_mint()
    ws = Ws(url, protocol="xai-client-secret." + secret)
    ws.send_text(json.dumps(session_update(plan)))
    deadline = time.time() + 8
    kinds = []
    while time.time() < deadline and len(kinds) < 4:
        msg = ws.recv(0.5)
        if not msg:
            continue
        opcode, payload = msg
        if opcode == 1:
            ev = json.loads(payload.decode())
            kinds.append(ev.get("type", "?"))
            if ev.get("type") in ("session.updated", "error"):
                break
    ws.close()
    print("ws", ",".join(kinds) if kinds else "no-event")
    if not kinds or kinds[-1] == "error":
        sys.exit(1)


def amplify(pcm: bytes) -> bytes:
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    for i, sample in enumerate(src):
        value = sample * MIC_GAIN
        if value > 32767:
            value = 32767
        elif value < -32768:
            value = -32768
        src[i] = value
    return src.tobytes()


def beep(spk, which):
    pcm = handset(which)
    spk.sendall(pcm)
    return (len(pcm) / 2) / OUT_RATE + TRACK_LAG + 0.08


def drain(sock):
    try:
        while sock.recv(65536):
            pass
    except BlockingIOError:
        pass


def raw_rms(pcm: bytes) -> int:
    n = len(pcm) - (len(pcm) % 2)
    if n < 2:
        return 0
    acc = 0
    count = n // 2
    for i in range(0, n, 2):
        sample = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        acc += sample * sample
    return int((acc / count) ** 0.5)


def mute_action(call_open, speaking):
    """Mute opens a call, cuts a reply that is still playing, or hangs up."""
    if not call_open:
        return "open"
    if speaking:
        return "cut"
    return "hangup"


def reopen_spk(old):
    """Close the speaker socket so Playd flushes, then connect the new track."""
    try:
        old.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    try:
        old.close()
    except OSError:
        pass
    deadline = time.time() + 1
    last = None
    while time.time() < deadline:
        try:
            fresh = socket.create_connection(("127.0.0.1", SPK_PORT), 0.3)
        except OSError as exc:
            last = exc
            time.sleep(0.05)
            continue
        fresh.settimeout(5)
        return fresh
    raise RuntimeError("speaker" if last is None else type(last).__name__)


def _pump(proc, lines):
    for line in proc.stdout:
        lines.put(line)


def run():
    if not SERIAL:
        sys.exit("set DOT_IP")
    if not MINT_SSH or not MINT_KEY:
        sys.exit("set DOT_MINT_SSH and DOT_MINT_KEY")
    connect_adb()
    lock_adb_port()
    light_q = queue.Queue()
    threading.Thread(target=light_worker, args=(light_q,), daemon=True).start()
    light_q.put("idle")
    play_proc, dot_proc = ensure_dotd()
    mic = spk = None
    live = None
    lines = queue.Queue()

    threading.Thread(target=_pump, args=(play_proc, lines), daemon=True).start()
    threading.Thread(target=_pump, args=(dot_proc, lines), daemon=True).start()
    try:
        mic, spk = open_socks()
        beep(spk, "open")
        print("handset up. unmute opens the line. mute closes it.", flush=True)
        playing_until = 0.0
        speaker_free = 0.0
        holding = False
        up_factor = 3
        acc = b""
        mic_energy = 0
        mic_samples = 0
        mic_at = time.time()
        want_mute = False
        gate = GATE0
        drop_audio = 0.0
        barge_hits = 0
        light_at = time.time()
        call_gen = 0
        tool_q = queue.Queue()
        tool_done = queue.Queue()

        def tool_worker():
            while True:
                gen, call_id, name, raw, tool_call = tool_q.get()
                try:
                    output = fetch_tool(call_id, name, raw)
                    err = ""
                except Exception as exc:
                    output = json.dumps({"error": type(exc).__name__})
                    err = type(exc).__name__
                tool_done.put((gen, name, tool_call, output, err))

        threading.Thread(target=tool_worker, daemon=True).start()

        def take_lines():
            nonlocal want_mute
            while True:
                try:
                    line = lines.get_nowait()
                except queue.Empty:
                    return
                text = line.decode(errors="replace").strip()
                if text == "MUTE down":
                    want_mute = True
                elif text and not text.startswith("MUTE"):
                    print(text, flush=True)

        def hangup():
            nonlocal live, playing_until, speaker_free, holding, acc, want_mute, gate, drop_audio, barge_hits
            light_q.put("idle")
            if live is not None:
                live.close()
                live = None
            playing_until = 0.0
            speaker_free = 0.0
            holding = False
            acc = b""
            want_mute = False
            gate = GATE0
            drop_audio = 0.0
            barge_hits = 0
            # Let Playd flush the track before the close click is written.
            time.sleep(0.15)
            beep(spk, "close")
            print("closed", flush=True)

        def drain_tools():
            nonlocal gate
            while True:
                try:
                    gen, name, tool_call, output, err = tool_done.get_nowait()
                except queue.Empty:
                    return
                if err:
                    print("tool fail", name, err, flush=True)
                else:
                    print("tool", name, flush=True)
                if live is None or getattr(live, "gen", None) != gen:
                    continue
                live.send_text(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": tool_call,
                        "output": output,
                    },
                }))
                ask_reply("tool")

        def watch_mute():
            take_lines()
            drain_tools()
            return want_mute and live is not None

        def ask_reply(event):
            nonlocal gate
            gate, send = reply_gate(gate, event)
            if not send or live is None:
                return
            if send == "nudge":
                frame = {
                    "type": "response.create",
                    "response": {"instructions": SPEAK_AGAIN},
                }
                print("speak", flush=True)
            else:
                frame = {"type": "response.create"}
                print("reply", flush=True)
            live.send_text(json.dumps(frame))

        def cut(keep=b""):
            """Stop the reply that is playing and keep the call open."""
            nonlocal spk, speaker_free, playing_until, holding, acc, gate, drop_audio, barge_hits
            drop_audio = time.time() + 2.0
            gate = GATE0
            speaker_free = 0.0
            playing_until = 0.0
            holding = False
            barge_hits = 0
            if live is not None:
                try:
                    live.send_text(json.dumps({"type": "response.cancel"}))
                except Exception:
                    pass
            try:
                spk = reopen_spk(spk)
            except Exception as exc:
                print("flush fail", type(exc).__name__, flush=True)
            drain(mic)
            acc = amplify(keep) if keep else b""
            print("cut", flush=True)

        while play_proc.poll() is None and dot_proc.poll() is None:
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                line = None
            if line:
                text = line.decode(errors="replace").strip()
                if text == "MUTE down":
                    want_mute = True
                elif text and not text.startswith("MUTE"):
                    print(text, flush=True)
            drain_tools()
            if live is None and time.time() - light_at >= 30:
                light_at = time.time()
                light_q.put("idle")
            if want_mute:
                want_mute = False
                if live is None:
                    try:
                        drain(mic)
                        acc = b""
                        live = start_call()
                        call_gen += 1
                        live.gen = call_gen
                    except Exception as exc:
                        live = None
                        beep(spk, "close")
                        print("voice refused", type(exc).__name__, flush=True)
                        continue
                    gate = GATE0
                    drop_audio = 0.0
                    barge_hits = 0
                    light_q.put("talk")
                    playing_until = time.time() + beep(spk, "open")
                    print("open", flush=True)
                elif mute_action(True, time.time() < speaker_free) == "cut":
                    cut()
                    continue
                else:
                    hangup()
                    continue
            if live is None:
                continue
            now = time.time()
            # ponytail: drop quiet mic audio while the speaker is in flight.
            # No AEC. A loud stretch cuts the reply. Upgrade path is the HAL
            # echo canceller, which would let a normal voice cut it too.
            if now < playing_until:
                holding = True
                try:
                    overheard = mic.recv(3200)
                except BlockingIOError:
                    overheard = b""
                if overheard and raw_rms(overheard) >= BARGE_RMS:
                    barge_hits += 1
                else:
                    barge_hits = 0
                if barge_hits >= 2:
                    cut(overheard)
            else:
                # The mute only stops new reads. Audio from the speaker is still
                # sitting in the mic socket, and uploading that backlog starts
                # another turn. Drop it once, then read live samples.
                if holding:
                    drain(mic)
                    acc = b""
                    mic_energy = 0
                    mic_samples = 0
                    holding = False
                try:
                    chunk = mic.recv(3200)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    acc += amplify(chunk)
                    span = chunk[: len(chunk) - (len(chunk) % 32)]
                    for i in range(0, len(span), 32):
                        sample = int.from_bytes(span[i:i + 2], "little", signed=True)
                        mic_energy += sample * sample
                        mic_samples += 1
                    if len(acc) >= 3200:
                        live.send_bin(to_api(acc[:3200]))
                        acc = acc[3200:]
                if time.time() - mic_at >= 1:
                    rms = int((mic_energy / mic_samples) ** 0.5) if mic_samples else 0
                    print(f"mic rms={rms}", flush=True)
                    mic_energy = 0
                    mic_samples = 0
                    mic_at = time.time()
            msg = live.recv(0)
            if not msg:
                continue
            opcode, payload = msg
            if opcode == 2 and payload:
                if drop_audio and time.time() < drop_audio:
                    continue
                drop_audio = 0.0
                pcm = upsample(payload, up_factor)
                if not send_pcm(spk, pcm, watch_mute):
                    if want_mute and live is not None:
                        want_mute = False
                        cut()
                    continue
                ask_reply("audio")
                rate = OUT_RATE // up_factor
                # Stack queued audio. A short chunk must not end the mute early.
                now_play = time.time()
                speaker_free = max(now_play, speaker_free) + (len(payload) / 2) / rate
                playing_until = speaker_free + 0.8
            elif opcode == 1:
                ev = json.loads(payload.decode())
                kind = ev.get("type", "")
                if kind not in ("response.output_audio_transcript.delta", "ping"):
                    print(kind, flush=True)
                text = ev.get("transcript") or ""
                if text and kind in (
                    "conversation.item.input_audio_transcription.completed",
                    "response.output_audio_transcript.done",
                ):
                    print(("heard" if "input" in kind else "said"), text[:240], flush=True)
                if kind in ("session.updated", "session.created"):
                    try:
                        rate = int(ev["session"]["audio"]["output"]["format"]["rate"])
                    except (KeyError, TypeError, ValueError):
                        rate = 0
                    if rate in (16000, 24000, 48000):
                        up_factor = OUT_RATE // rate
                if kind in ("response.done", "response.cancelled", "response.canceled"):
                    drop_audio = 0.0
                if kind in ("response.output_audio.delta", "response.audio.delta") and ev.get("delta"):
                    if drop_audio and time.time() < drop_audio:
                        pcm = b""
                    else:
                        drop_audio = 0.0
                        try:
                            pcm = base64.b64decode(ev["delta"])
                        except Exception:
                            pcm = b""
                    if pcm:
                        out = upsample(pcm, up_factor)
                        if not send_pcm(spk, out, watch_mute):
                            if want_mute and live is not None:
                                want_mute = False
                                cut()
                            continue
                        ask_reply("audio")
                        rate = OUT_RATE // up_factor
                        now_play = time.time()
                        speaker_free = max(now_play, speaker_free) + (len(pcm) / 2) / rate
                        playing_until = speaker_free + 0.8
                if kind in ("response.created", "response.done"):
                    ask_reply("created" if kind == "response.created" else "done")
                if kind == "response.function_call_arguments.done":
                    tool_name = ev.get("name") or ""
                    tool_call = ev.get("call_id") or ""
                    if tool_name and tool_call:
                        # The app speaks the harness receipt while the tool
                        # runs. A bare create after the tool comes back is
                        # the gap the Dot had.
                        receipt = getattr(live, "receipt", "") or ""
                        inflight, pending, _owed = gate
                        if receipt and not inflight and not pending:
                            live.send_text(json.dumps({
                                "type": "response.create",
                                "response": {"instructions": receipt},
                            }))
                            gate = (0, True, False, False, False, False)
                            print("receipt", flush=True)
                        tool_q.put((
                            live.gen,
                            live.call_id,
                            tool_name,
                            ev.get("arguments") or "{}",
                            tool_call,
                        ))
                if kind == "error":
                    err = ev.get("error") or {}
                    print("voice error", err.get("code") or err.get("type") or "error", flush=True)
    finally:
        if live:
            live.close()
        if mic:
            mic.close()
        if spk:
            spk.close()
        play_proc.kill()
        dot_proc.kill()


def start_call():
    url, secret, plan = fetch_mint()
    ws = Ws(url, protocol="xai-client-secret." + secret)
    ws.call_id = "call-" + str(uuid.uuid4())
    ws.receipt = plan.get("receipt") or ""
    ws.send_text(json.dumps(session_update(plan)))
    return ws


def beep_once():
    """Prove the speaker and the mic, then exit. No voice session."""
    connect_adb()
    play_proc, dot_proc = ensure_dotd()
    mic = spk = None
    try:
        mic, spk = open_socks()
        beep(spk, "open")
        time.sleep(0.4)
        deadline = time.time() + 0.5
        got = 0
        acc = 0
        samples = 0
        while time.time() < deadline:
            try:
                chunk = mic.recv(3200)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            if not chunk:
                break
            got += len(chunk)
            step = chunk[: len(chunk) - (len(chunk) % 2)]
            for i in range(0, len(step), 2):
                sample = int.from_bytes(step[i:i + 2], "little", signed=True)
                acc += sample * sample
                samples += 1
        rms = int((acc / samples) ** 0.5) if samples else 0
        print(f"beep_sent mic_bytes={got} rms={rms}")
        if got < 1000:
            sys.exit(1)
    finally:
        if mic:
            mic.close()
        if spk:
            spk.close()
        play_proc.kill()
        dot_proc.kill()


def self_check():
    pcm = array.array("h", [1, -2, 3]).tobytes()
    got = up3(pcm)
    assert len(got) == 18, len(got)
    flat = upsample(array.array("h", [100, 100]).tobytes(), 3)
    assert flat == array.array("h", [100, 100, 100, 100, 100, 100]).tobytes()
    tool = {
        "type": "function",
        "name": "send_task",
        "description": "d",
        "parameters": {"type": "object", "properties": {}},
    }
    body = session_update({
        "instructions": "test-line",
        "tools": [tool],
        "voice": "cosmo",
        "speed": 1,
        "language": "auto",
    })["session"]
    assert body["instructions"] == "test-line"
    assert body["voice"] == "cosmo"
    assert body["tools"][0]["name"] == "send_task"
    assert body["resumption"]["enabled"] is True
    assert body["audio"]["input"]["transport"] == "binary"
    assert body["audio"]["input"]["format"]["rate"] == 24000
    assert body["audio"]["output"]["format"]["rate"] == 24000
    assert body["audio"]["output"]["speed"] == 1
    assert body["audio"]["input"]["transcription"] == {"model": "grok-transcribe"}
    assert "language_hint" not in body["audio"]["input"]["transcription"]
    assert body["turn_detection"]["type"] == "server_vad"
    omitted = session_update({
        "instructions": "test-line",
        "tools": [tool],
        "voice": None,
        "speed": 1.25,
        "language": "fr",
    })["session"]
    assert "voice" not in omitted
    assert omitted["audio"]["output"]["speed"] == 1.25
    assert omitted["audio"]["input"]["transcription"]["language_hint"] == "fr"
    heard = array.array("h")
    heard.frombytes(to_api(array.array("h", [0, 300]).tobytes()))
    assert list(heard) == [0, 200, 300]
    assert len(to_api(array.array("h", [1000] * 1600).tobytes())) == 4800
    try:
        session_update({})
    except RuntimeError:
        pass
    else:
        raise SystemExit("empty plan must fail")
    left, right = socket.socketpair()
    right.setblocking(False)
    assert send_pcm(left, b"\x01\x00" * 8, lambda: False) is True
    assert send_pcm(left, b"\x01\x00" * 8, lambda: True) is False
    left.close()
    right.close()
    loud = array.array("h")
    loud.frombytes(amplify(array.array("h", [1000, -1000, 20000]).tobytes()))
    assert list(loud) == [6000, -6000, 32767]
    state = GATE0
    state, send = reply_gate(state, "tool")
    assert send == "tool" and state == (0, True, False, True, False, False)
    state, send = reply_gate(state, "tool")
    assert not send and state == (0, True, True, True, False, False)
    state, send = reply_gate(state, "created")
    assert not send and state == (1, False, True, True, False, False)
    state, send = reply_gate(state, "done")
    assert send == "tool" and state == (0, True, False, True, False, False)
    state = (1, False, False, False, False, False)
    state, send = reply_gate(state, "tool")
    assert not send and state == (1, False, True, False, False, False)
    state, send = reply_gate(state, "tool")
    assert not send and state == (1, False, True, False, False, False)
    state, send = reply_gate(state, "done")
    assert send == "tool" and state == (0, True, False, True, False, False)
    state, send = reply_gate(state, "created")
    assert not send and state == (1, False, False, True, False, False)
    state, send = reply_gate(state, "audio")
    state, send = reply_gate(state, "done")
    assert not send and state == (0, False, False, False, True, False)
    state = GATE0
    state, send = reply_gate(state, "tool")
    state, send = reply_gate(state, "created")
    state, send = reply_gate(state, "done")
    assert send == "nudge" and state == (0, True, False, False, False, True)
    state, send = reply_gate(state, "created")
    state, send = reply_gate(state, "done")
    assert not send and state == (0, False, False, False, False, True)
    state = GATE0
    state, send = reply_gate(state, "created")
    state, send = reply_gate(state, "done")
    assert not send and state == GATE0
    assert mute_action(False, True) == "open"
    assert mute_action(True, True) == "cut"
    assert mute_action(True, False) == "hangup"
    assert raw_rms(b"") == 0
    assert raw_rms(array.array("h", [1600, -1600]).tobytes()) == 1600
    opened = handset("open")
    closed = handset("close")
    assert opened != closed and 0 < len(opened) < len(closed) < len(tone(440, 400))
    assert int.from_bytes(opened[:2], "little", signed=True) == 0
    peak = max(
        abs(int.from_bytes(opened[i:i + 2], "little", signed=True))
        for i in range(0, len(opened), 2)
    )
    assert 2000 < peak <= 32767
    assert light_cmds("talk") == ("/system/bin/ledctrl --set_pattern=active-talking",)
    idle = light_cmds("idle")
    assert idle[0].endswith("--unset_pattern=active-talking")
    assert idle[1].endswith("--unset_pattern=anim_OOBE_start_error")
    try:
        light_cmds("nope")
    except RuntimeError:
        pass
    else:
        raise SystemExit("bad light")
    assert _ipv4("192.0.2.10")
    assert not _ipv4("192.0.2.10;id")
    assert not _ipv4("192.0.2")
    assert not _ipv4("01.2.3.4")
    digest = hashlib.sha256(tone(440, 100)).hexdigest()[:8]
    assert digest == "22c3ec84"
    print("self-check ok", digest)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
    elif "--check-ws" in sys.argv:
        check_ws()
    elif "--beep" in sys.argv:
        beep_once()
    else:
        run()
