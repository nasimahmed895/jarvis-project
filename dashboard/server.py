"""
dashboard/server.py — JARVIS Local HTTPS Dashboard

HTTPS on port 8000 with an auto-generated self-signed certificate (browsers
will show a cert warning on first visit — click through to proceed).
Extra application-layer crypto: AES-256-CBC with a session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── Task progress summariser (P3.3) ───────────────────────────────────────────

def _summarise_task(tasks_mod, row: dict) -> dict:
    """Shrink a task row + its steps into a progress-friendly payload.

    Produces `{id, goal, status, done, total, pct, updated_at, next_step}`
    so the dashboard can render a progress bar without pulling per-step
    detail for every active task."""
    try:
        steps = tasks_mod.task_steps(row["id"])
    except Exception:
        steps = []
    done = sum(1 for s in steps if s["status"] == "done")
    total = len(steps)
    running = next((s for s in steps if s["status"] == "running"), None)
    next_planned = next((s for s in steps if s["status"] in ("planned", "ready")), None)
    next_step = running or next_planned
    return {
        "id":         row["id"],
        "goal":       row.get("goal") or "",
        "status":     row["status"],
        "done":       done,
        "total":      total,
        "pct":        round(100.0 * done / total, 1) if total else 0.0,
        "updated_at": row.get("updated_at"),
        "next_step":  (next_step.get("verb") if next_step else None),
    }


# ── Per-IP token-bucket rate limiter ──────────────────────────────────────────
#
# In-process, dependency-free. Guards /api/* and /login against brute-force and
# noisy clients. Keyed by request.client.host. Buckets older than _RL_IDLE_SECS
# are reaped on insert so the dict cannot grow unbounded.

_RL_IDLE_SECS = 300.0


class _TokenBucket:
    """Classic token bucket: `capacity` tokens, refill at `rate`/sec."""
    __slots__ = ("capacity", "rate", "tokens", "last")

    def __init__(self, capacity: float, rate: float):
        self.capacity = capacity
        self.rate     = rate
        self.tokens   = capacity
        self.last     = time.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now  = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last   = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class _RateLimiter:
    """Per-IP, per-scope token buckets. `scope` picks the (capacity, rate) policy."""

    # scope → (capacity, refill_per_sec)
    POLICIES = {
        "api":   (60, 10.0),   # 10 req/s sustained, burst 60
        "login": (10,  1.0),   # 1  req/s sustained, burst 10 — brute-force guard
    }

    def __init__(self):
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._touched: dict[tuple[str, str], float] = {}

    def check(self, ip: str, scope: str) -> bool:
        key = (ip, scope)
        bucket = self._buckets.get(key)
        if bucket is None:
            cap, rate = self.POLICIES.get(scope, self.POLICIES["api"])
            bucket = _TokenBucket(cap, rate)
            self._buckets[key] = bucket
            self._reap()
        self._touched[key] = time.monotonic()
        return bucket.allow()

    def _reap(self) -> None:
        if len(self._buckets) < 256:
            return
        cutoff = time.monotonic() - _RL_IDLE_SECS
        stale  = [k for k, t in self._touched.items() if t < cutoff]
        for k in stale:
            self._buckets.pop(k, None)
            self._touched.pop(k, None)


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self._rate_limiter                = _RateLimiter()
        self._started_at                  = time.time()
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        # ── rate-limit middleware: /api/* and /login (per client IP) ────────
        @app.middleware("http")
        async def _rate_limit(request: "Request", call_next):
            path = request.url.path
            scope = None
            if path == "/login":
                scope = "login"
            elif path.startswith("/api/"):
                scope = "api"
            if scope is not None:
                ip = request.client.host if request.client else "unknown"
                if not self._rate_limiter.check(ip, scope):
                    return JSONResponse(
                        {"error": "Rate limit exceeded"},
                        status_code=429,
                        headers={"Retry-After": "1"},
                    )
            return await call_next(request)

        @app.post("/webhook/{name}")
        async def inbound_webhook(name: str, request: "Request"):
            """P4.9 — HMAC-signed inbound webhooks. Register via
            core.webhooks.default_registry().register()."""
            from core.webhooks import default_registry
            reg = default_registry()
            body = await request.body()
            sig = request.headers.get("X-JARVIS-Signature")
            if not reg.verify(name, body, sig):
                return JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                )
            try:
                headers = dict(request.headers)
                result = reg.dispatch(name, body, headers)
            except KeyError:
                return JSONResponse(
                    {"error": "Unknown webhook"}, status_code=404,
                )
            except Exception as e:
                return JSONResponse(
                    {"error": f"{type(e).__name__}: {e}"}, status_code=500,
                )
            if isinstance(result, dict):
                return JSONResponse({"ok": True, "result": result})
            return JSONResponse({"ok": True, "result": str(result) if result is not None else None})

        @app.get("/health")
        async def health():
            return JSONResponse({
                "status":   "ok",
                "uptime_s": round(time.time() - self._started_at, 1),
                "clients":  len(self._clients),
                "tokens":   len(self._tokens),
            })

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/dashboard", response_class=HTMLResponse)
        async def dashboard_preview():
            path = STATIC_DIR / "jarvis-dashboard.html"
            if not path.exists():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return HTMLResponse(path.read_text(encoding="utf-8"))

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered
                self._aes_key(entered)                   # pre-derive & cache
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Read-only data API (command-center dashboard) ────────────────────
        # All endpoints are auth-gated and import JARVIS modules lazily so
        # dashboard still serves even if a given backend module fails to load.

        _MEM_DIR = BASE_DIR / "memory"

        @app.get("/api/data/status")
        async def data_status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({
                "ok": True,
                "server_time": time.time(),
                "ip": self._ip,
                "port": PORT,
                "clients": len(self._clients),
                "history": len(self._history),
                "queued_commands": self._command_queue.qsize(),
                "devices": len(self._device_sessions),
                "uploads_dir": str(self._uploads_dir),
                "ssl": self._ssl_enabled(),
            })

        @app.get("/api/data/history")
        async def data_history(req: Request, limit: int = 100):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            n = max(1, min(int(limit), 300))
            return JSONResponse({"ok": True, "items": self._history[-n:]})

        @app.get("/api/data/overview")
        async def data_overview(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out = {
                "ok": True,
                "generated_at": time.time(),
                "memory":      {"total": 0, "categories": {}},
                "tasks":       {"count": 0, "by_status": {}},
                "todos":       {"open": 0, "done": 0},
                "projects":    {"count": 0, "active": 0},
                "commitments": {"open": 0, "due_24h": 0},
                "journal":     {"today": 0},
                "vectors":     {"count": 0},
                "contacts":    {"count": 0, "follow_ups": 0},
                "trips":       {"upcoming": 0},
            }
            try:
                from memory import memory_manager
                mem = memory_manager.load_memory() or {}
                cats = {}
                total = 0
                for k, v in mem.items():
                    if isinstance(v, dict):
                        cats[k] = len(v);     total += len(v)
                    elif isinstance(v, list):
                        cats[k] = len(v);     total += len(v)
                    elif v is not None:
                        cats[k] = 1;          total += 1
                out["memory"] = {"total": total, "categories": cats}
            except Exception:
                pass
            try:
                from core import tasks as _t
                _t.connect(_MEM_DIR / "tasks.sqlite")
                rows = _t.list_tasks(limit=500)
                by = {}
                for r in rows:
                    s = r.get("status", "unknown")
                    by[s] = by.get(s, 0) + 1
                out["tasks"] = {"count": len(rows), "by_status": by}
            except Exception:
                pass
            try:
                import sqlite3
                p = _MEM_DIR / "todos.sqlite"
                if p.exists():
                    cn = sqlite3.connect(str(p))
                    for d, n in cn.execute("SELECT done, COUNT(*) FROM todos GROUP BY done"):
                        out["todos"]["done" if d else "open"] = n
                    cn.close()
            except Exception:
                pass
            try:
                from core import projects as _p
                _p.connect(_MEM_DIR / "projects.sqlite")
                rows = _p.list_projects(limit=500)
                out["projects"] = {
                    "count": len(rows),
                    "active": sum(1 for r in rows if r.get("status") == "active"),
                }
            except Exception:
                pass
            try:
                from core import commitments as _c
                _c.connect(_MEM_DIR / "commitments.sqlite")
                out["commitments"] = {
                    "open": len(_c.list_open(limit=500)),
                    "due_24h": len(_c.due_within(24)),
                }
            except Exception:
                pass
            try:
                from core import journal as _j
                _j.connect(_MEM_DIR / "journal")
                out["journal"]["today"] = len(_j.read_recent(limit=500))
            except Exception:
                pass
            try:
                from core import vectors as _v
                _v.connect(_MEM_DIR / "vectors.sqlite")
                out["vectors"]["count"] = _v.count()
            except Exception:
                pass
            try:
                from core import crm as _cr
                _cr.connect(_MEM_DIR / "crm.sqlite")
                out["contacts"] = {
                    "count": len(_cr.list_contacts(limit=500)),
                    "follow_ups": len(_cr.due_follow_ups(within_hours=72)),
                }
            except Exception:
                pass
            try:
                from core import travel as _tr
                _tr.connect(_MEM_DIR / "travel.sqlite")
                out["trips"]["upcoming"] = len(_tr.upcoming_trips(within_days=90))
            except Exception:
                pass
            return JSONResponse(out)

        @app.get("/api/data/tasks")
        async def data_tasks(req: Request, limit: int = 100):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import tasks as _t
                _t.connect(_MEM_DIR / "tasks.sqlite")
                n = max(1, min(int(limit), 500))
                return JSONResponse({"ok": True, "items": _t.list_tasks(limit=n)})
            except Exception as e:
                return JSONResponse({"ok": True, "items": [], "error": str(e)})

        # ── Browser automation panel ──────────────────────────────────────
        # Surfaces: tasks whose plan touches the browser, the latest
        # screenshot the model saved, and a simple pause/resume/stop hook.
        # The real browser session lives in actions/browser_control.py; this
        # endpoint only READS state — control actions go through /api/command
        # so the Live session executes them with full gating.
        _BROWSER_TOOLS = ("browser_control", "browser_flow", "jobs",
                          "real_estate", "education", "customer_support",
                          "social_media", "flight_finder", "travel",
                          "market_research", "web_search", "morning_briefing")

        def _browser_touching(tasks_mod, row: dict) -> bool:
            try:
                steps = tasks_mod.task_steps(row["id"])
            except Exception:
                return False
            return any((s.get("tool") or "") in _BROWSER_TOOLS for s in steps)

        def _latest_screenshot() -> dict | None:
            shots_dir = _MEM_DIR.parent / "logs" / "screenshots"
            if not shots_dir.exists():
                return None
            try:
                latest = max(shots_dir.glob("*.png"),
                             key=lambda p: p.stat().st_mtime, default=None)
            except Exception:
                return None
            if latest is None:
                return None
            st = latest.stat()
            return {
                "path":       str(latest),
                "name":       latest.name,
                "mtime":      st.st_mtime,
                "size_bytes": st.st_size,
            }

        @app.get("/api/data/browser")
        async def data_browser(req: Request, limit: int = 25):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import tasks as _t
                _t.connect(_MEM_DIR / "tasks.sqlite")
                n = max(1, min(int(limit), 100))
                all_tasks = _t.list_tasks(limit=n * 3)
                items = [_summarise_task(_t, r) for r in all_tasks
                         if _browser_touching(_t, r)][:n]
                # Running task — the most recent one in `running` state, if any.
                active = next((r for r in all_tasks
                               if r.get("status") == "running"
                               and _browser_touching(_t, r)), None)
                active_steps = []
                if active:
                    try:
                        active_steps = _t.task_steps(active["id"])
                    except Exception:
                        pass
                return JSONResponse({
                    "ok":       True,
                    "items":    items,
                    "active":   _summarise_task(_t, active) if active else None,
                    "active_steps": [
                        {"idx": s.get("idx"),
                         "verb": s.get("verb"),
                         "tool": s.get("tool"),
                         "status": s.get("status"),
                         "error": (s.get("error") or "")[:200]}
                        for s in active_steps
                    ],
                    "screenshot": _latest_screenshot(),
                })
            except Exception as e:
                return JSONResponse({"ok": True, "items": [], "error": str(e)})

        @app.post("/api/data/browser/stop")
        async def data_browser_stop(req: Request):
            """Mark all currently-running browser-touching tasks as cancelled.
            The worker's next tick observes the state change and stops
            dispatching further steps. Running steps complete naturally —
            this is the safe way to stop; it does not kill a mid-flight HTTP
            call."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import tasks as _t
                _t.connect(_MEM_DIR / "tasks.sqlite")
                cancelled = []
                for r in _t.list_tasks(status="running", limit=100):
                    if _browser_touching(_t, r):
                        _t.set_task_status(r["id"], "cancelled",
                                           "Stopped from dashboard.")
                        cancelled.append(r["id"])
                return JSONResponse({"ok": True, "cancelled": cancelled})
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        @app.get("/api/v1/metrics")
        async def v1_metrics(req: Request):
            """P3.4 — CPU / RAM / disk snapshot for the health panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            metrics: dict = {
                "uptime_s": round(time.time() - self._started_at, 1),
            }
            try:
                import psutil
                metrics["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
                vm = psutil.virtual_memory()
                metrics["ram"] = {
                    "percent":   round(vm.percent, 1),
                    "used_mb":   round(vm.used / (1024 * 1024)),
                    "total_mb":  round(vm.total / (1024 * 1024)),
                }
                disk = psutil.disk_usage("/")
                metrics["disk"] = {
                    "percent":   round(disk.percent, 1),
                    "used_gb":   round(disk.used / (1024 ** 3), 1),
                    "total_gb":  round(disk.total / (1024 ** 3), 1),
                }
                metrics["process_count"] = len(psutil.pids())
            except Exception as e:
                metrics["error"] = f"{type(e).__name__}: {e}"
            return JSONResponse(metrics)

        # ── v1 task endpoints (P3.3): progress bars + history view ───────

        @app.get("/api/v1/tasks/progress")
        async def v1_tasks_progress(req: Request, limit: int = 20):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import tasks as _t
                _t.connect(_MEM_DIR / "tasks.sqlite")
                n = max(1, min(int(limit), 100))
                rows = _t.list_tasks(
                    status=["planned", "running", "awaiting_user"],
                    limit=n,
                )
                items = [_summarise_task(_t, r) for r in rows]
                return JSONResponse({"ok": True, "items": items})
            except Exception as e:
                return JSONResponse({"ok": True, "items": [], "error": str(e)})

        @app.get("/api/v1/tasks/history")
        async def v1_tasks_history(req: Request, limit: int = 50,
                                    status: str = "done,failed,cancelled"):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import tasks as _t
                _t.connect(_MEM_DIR / "tasks.sqlite")
                n = max(1, min(int(limit), 200))
                wanted = [s.strip() for s in status.split(",") if s.strip()]
                rows = _t.list_tasks(status=wanted, limit=n)
                items = [_summarise_task(_t, r) for r in rows]
                return JSONResponse({"ok": True, "items": items})
            except Exception as e:
                return JSONResponse({"ok": True, "items": [], "error": str(e)})

        @app.get("/api/v1/tasks/{task_id}")
        async def v1_task_detail(req: Request, task_id: int):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import tasks as _t
                _t.connect(_MEM_DIR / "tasks.sqlite")
                t = _t.get_task(int(task_id))
                if not t:
                    return JSONResponse({"error": "Not found"}, status_code=404)
                steps = _t.task_steps(int(task_id))
                return JSONResponse({"ok": True, "task": t, "steps": steps})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/data/todos")
        async def data_todos(req: Request, limit: int = 200):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                import sqlite3
                p = _MEM_DIR / "todos.sqlite"
                if not p.exists():
                    return JSONResponse({"ok": True, "items": []})
                cn = sqlite3.connect(str(p))
                cn.row_factory = sqlite3.Row
                n = max(1, min(int(limit), 1000))
                rows = cn.execute(
                    "SELECT id, list, text, done, due, priority, created_at, done_at "
                    "FROM todos ORDER BY done ASC, priority DESC, created_at DESC LIMIT ?",
                    (n,),
                ).fetchall()
                cn.close()
                return JSONResponse({"ok": True, "items": [dict(r) for r in rows]})
            except Exception as e:
                return JSONResponse({"ok": True, "items": [], "error": str(e)})

        @app.get("/api/data/projects")
        async def data_projects(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out = []
            try:
                from core import projects as _p
                _p.connect(_MEM_DIR / "projects.sqlite")
                for row in _p.list_projects(limit=200):
                    try:
                        summ = _p.project_summary(row["id"])
                    except Exception:
                        summ = {}
                    out.append({"project": row, "summary": summ})
            except Exception:
                pass
            return JSONResponse({"ok": True, "items": out})

        @app.get("/api/data/commitments")
        async def data_commitments(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out = {"open": [], "due_24h": []}
            try:
                from core import commitments as _c
                _c.connect(_MEM_DIR / "commitments.sqlite")
                out["open"] = _c.list_open(limit=200)
                out["due_24h"] = _c.due_within(24)
            except Exception:
                pass
            return JSONResponse({"ok": True, **out})

        @app.get("/api/data/memory")
        async def data_memory(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from memory import memory_manager as _m
                items = _m.all_entries_for_ui()
                return JSONResponse({"ok": True, "items": items})
            except Exception as e:
                return JSONResponse({"ok": True, "items": [], "error": str(e)})

        @app.get("/api/data/journal")
        async def data_journal(req: Request, limit: int = 100):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import journal as _j
                _j.connect(_MEM_DIR / "journal")
                n = max(1, min(int(limit), 500))
                return JSONResponse({"ok": True, "items": _j.read_recent(limit=n)})
            except Exception as e:
                return JSONResponse({"ok": True, "items": [], "error": str(e)})

        @app.get("/api/data/contacts")
        async def data_contacts(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out = {"contacts": [], "follow_ups": [], "stats": {}}
            try:
                from core import crm as _cr
                _cr.connect(_MEM_DIR / "crm.sqlite")
                out["contacts"] = _cr.list_contacts(limit=200)
                out["follow_ups"] = _cr.due_follow_ups(within_hours=72)
                out["stats"] = _cr.stats()
            except Exception:
                pass
            return JSONResponse({"ok": True, **out})

        @app.get("/api/data/finance")
        async def data_finance(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            import datetime as _dt
            now = _dt.datetime.now()
            out = {"expenses": [], "monthly": {}, "budgets": [],
                   "bills": [], "subs": [], "sub_monthly": {}}
            try:
                from core import finance as _f
                _f.connect(_MEM_DIR / "finance.sqlite")
                out["expenses"]    = _f.list_expenses(limit=50)
                out["monthly"]     = _f.monthly_summary(now.year, now.month)
                out["budgets"]     = _f.budget_status(now.year, now.month)
                out["bills"]       = _f.upcoming_bills(within_days=14)
                out["subs"]        = _f.active_subscriptions()
                out["sub_monthly"] = _f.monthly_subscription_cost()
            except Exception:
                pass
            return JSONResponse({"ok": True, **out})

        @app.get("/api/data/trips")
        async def data_trips(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out = {"all": [], "upcoming": []}
            try:
                from core import travel as _tr
                _tr.connect(_MEM_DIR / "travel.sqlite")
                out["all"]      = _tr.list_trips(limit=50)
                out["upcoming"] = _tr.upcoming_trips(within_days=90)
            except Exception:
                pass
            return JSONResponse({"ok": True, **out})

        @app.get("/api/data/vectors")
        async def data_vectors(req: Request, q: str = "", limit: int = 10):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out = {"count": 0, "results": []}
            try:
                from core import vectors as _v
                _v.connect(_MEM_DIR / "vectors.sqlite")
                out["count"] = _v.count()
                q = (q or "").strip()
                if q:
                    out["results"] = _v.search(q, top_k=max(1, min(int(limit), 20)))
            except Exception:
                pass
            return JSONResponse({"ok": True, **out})

        @app.get("/api/data/modules")
        async def data_modules(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            def _enum(path):
                try:
                    return sorted(
                        p.stem for p in path.glob("*.py")
                        if not p.stem.startswith("_") and p.stem != "__init__"
                    )
                except Exception:
                    return []
            return JSONResponse({
                "ok": True,
                "actions": _enum(BASE_DIR / "actions"),
                "plugins": _enum(BASE_DIR / "plugins"),
                "core":    _enum(BASE_DIR / "core"),
            })

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
