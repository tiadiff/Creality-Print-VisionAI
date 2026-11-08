"""
Creality Print Vision AI: Lightweight Local Server Supervisor (:8089)

Allows the Creality Print UI (`upscale.js`) to inspect status, start, stop,
restart, and view logs for the local AI server (`:8088`, either Full GPU
Real-ESRGAN + Stringing AI or Standalone CPU Telemetry + Stringing AI) directly
from Chromium without opening manual terminals.

Security:
  - Binds strictly to 127.0.0.1 (never exposed to LAN).
  - Enforces localhost/loopback Origin pinning on mutating endpoints.
  - Uses only Python standard library (~12 MB RAM, 0% CPU idle).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

CTRL_HOST = "127.0.0.1"
CTRL_PORT = 8089
AI_HOST = "127.0.0.1"
AI_PORT = 8088

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
    "CrealityVisionAI",
    "logs",
)

_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d{1,5})?$", re.IGNORECASE)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _is_allowed_origin(origin: Optional[str]) -> bool:
    if not origin:
        return True
    o = origin.strip()
    if o == "null" or o.startswith("file://"):
        return True
    return bool(_LOCAL_ORIGIN_RE.match(o))


def _resolve_python_exe(candidate: Optional[str] = None) -> str:
    """Resolve console `python.exe` even when supervisor is hosted inside `pythonw.exe`."""
    exe = candidate or sys.executable
    if exe:
        base = os.path.basename(exe).lower()
        if base == "pythonw.exe":
            sibling = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(sibling):
                exe = sibling
        if os.path.isfile(exe):
            return exe
    for name in ("python", "py"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


def _resolve_pythonw_exe(candidate: Optional[str] = None) -> str:
    exe = candidate or sys.executable
    if exe:
        base = os.path.basename(exe).lower()
        if base == "python.exe":
            sibling = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(sibling):
                return sibling
        if os.path.isfile(exe):
            return exe
    return shutil.which("pythonw") or sys.executable


def _python_has_modules(py_exe: str, modules: Tuple[str, ...]) -> bool:
    if not py_exe or not os.path.isfile(py_exe):
        return False
    # Fast direct check if checking current interpreter
    try:
        if os.path.abspath(py_exe).lower() == os.path.abspath(_resolve_python_exe()).lower():
            return all(importlib.util.find_spec(m) is not None for m in modules)
    except Exception:
        pass
    code = (
        "import importlib.util, sys; "
        f"mods = {list(modules)!r}; "
        "sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in mods) else 1)"
    )
    try:
        res = subprocess.run(
            [py_exe, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.5,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return res.returncode == 0
    except Exception:
        return False


class _ReuseThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class AIServerSupervisor:
    """Thread-safe controller managing the `:8088` AI / Telemetry child process."""

    def __init__(
        self,
        repo_dir: str = REPO_DIR,
        ai_host: str = AI_HOST,
        ai_port: int = AI_PORT,
        ctrl_port: int = CTRL_PORT,
        log_dir: str = DEFAULT_LOG_DIR,
        python_override: Optional[str] = None,
    ):
        self.repo_dir = os.path.abspath(repo_dir)
        self.ai_host = ai_host
        self.ai_port = int(ai_port)
        self.ctrl_port = int(ctrl_port)
        self.log_dir = log_dir
        self.log_path = os.path.join(self.log_dir, "ai_server.log")
        self.python_override = python_override

        self._op_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.started_at: float = 0.0
        self.active_mode: str = "stopped"
        self.desired_mode: str = "auto"
        self.last_error: str = ""
        self.last_exit_code: Optional[int] = None
        self._cap_cache: Optional[Tuple[float, Dict[str, Any]]] = None

    def inspect_capabilities(self, refresh: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        with self._state_lock:
            if not refresh and self._cap_cache and (now - self._cap_cache[0]) < 15.0:
                return dict(self._cap_cache[1])

        venv_py = os.path.join(self.repo_dir, ".venv", "Scripts", "python.exe")
        if os.name != "nt" and not os.path.isfile(venv_py):
            venv_py = os.path.join(self.repo_dir, ".venv", "bin", "python")
        has_venv = os.path.isfile(venv_py)

        onnx_path = os.path.join(self.repo_dir, "models", "realesr_animevideo_v3_fused.onnx")
        has_onnx = os.path.isfile(onnx_path)

        full_script = os.path.join(self.repo_dir, "ai_upscaler_service.py")
        standalone_script = os.path.join(self.repo_dir, "printer_telemetry.py")

        standalone_py = self.python_override or (venv_py if has_venv else _resolve_python_exe())
        has_full_gpu = bool(
            has_venv
            and has_onnx
            and os.path.isfile(full_script)
            and _python_has_modules(venv_py, ("aiohttp", "onnxruntime", "cv2", "numpy"))
        )
        can_stringing_ai = bool(
            os.path.isfile(standalone_script)
            and _python_has_modules(standalone_py, ("cv2", "numpy"))
        )

        caps = {
            "has_venv": has_venv,
            "has_onnx_model": has_onnx,
            "has_full_gpu": has_full_gpu,
            "can_stringing_ai": can_stringing_ai,
            "full_python": venv_py if has_venv else None,
            "standalone_python": standalone_py,
            "full_script_exists": os.path.isfile(full_script),
            "standalone_script_exists": os.path.isfile(standalone_script),
        }
        with self._state_lock:
            self._cap_cache = (now, dict(caps))
        return caps

    def probe_ai_server(self, timeout: float = 0.65) -> Optional[Dict[str, Any]]:
        # Fast loopback TCP check avoids 0.5s+ urllib connect/retry lag on closed Windows ports
        tcp_timeout = min(0.18, max(0.08, float(timeout) * 0.4))
        try:
            with socket.create_connection((self.ai_host, self.ai_port), timeout=tcp_timeout):
                pass
        except Exception:
            return None

        url = f"http://{self.ai_host}:{self.ai_port}/status"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CrealityVisionAI-Supervisor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= int(resp.status) < 300:
                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return None

    def _reconcile_child_locked(self) -> None:
        if self.proc is not None:
            rc = self.proc.poll()
            if rc is not None:
                self.last_exit_code = int(rc)
                if rc != 0 and not self.last_error:
                    tail = self.read_logs(lines=6)
                    msg = " | ".join(line.strip() for line in tail[-3:] if line.strip())
                    self.last_error = msg or f"Processo terminato con codice {rc}"
                self.proc = None
                self.started_at = 0.0
                self.active_mode = "stopped"

    def get_status(self, refresh_caps: bool = False) -> Dict[str, Any]:
        caps = self.inspect_capabilities(refresh=refresh_caps)
        port_status = self.probe_ai_server(timeout=0.45)

        with self._state_lock:
            self._reconcile_child_locked()
            proc = self.proc
            managed = proc is not None
            child_pid = proc.pid if proc is not None else None
            started_at = self.started_at
            active_mode = self.active_mode
            desired_mode = self.desired_mode
            last_error = self.last_error
            last_exit_code = self.last_exit_code

        port_listening = port_status is not None
        ai_running = managed or port_listening
        starting = bool(managed and not port_listening)

        resolved_mode = "stopped"
        if port_listening:
            reported = str((port_status or {}).get("mode") or "")
            if reported in ("full_gpu", "standalone_cpu"):
                resolved_mode = reported
            elif (port_status or {}).get("telemetry_only") is True:
                resolved_mode = "standalone_cpu"
            elif isinstance((port_status or {}).get("stats"), dict):
                resolved_mode = "full_gpu"
            else:
                resolved_mode = active_mode if active_mode != "stopped" else "external"
        elif starting:
            resolved_mode = active_mode

        uptime_sec = 0.0
        if port_status and port_status.get("uptime_sec") is not None:
            try:
                uptime_sec = float(port_status["uptime_sec"])
            except Exception:
                uptime_sec = 0.0
        elif managed and started_at > 0:
            uptime_sec = max(0.0, time.time() - started_at)

        ai_pid = (port_status or {}).get("pid") or child_pid
        has_full_gpu = bool(
            caps["has_full_gpu"]
            or resolved_mode == "full_gpu"
            or (port_status and port_status.get("gpu_upscaler"))
        )

        return {
            "ok": True,
            "supervisor_online": True,
            "supervisor_pid": os.getpid(),
            "supervisor_port": self.ctrl_port,
            "ai_port": self.ai_port,
            "ai_running": ai_running,
            "starting": starting,
            "port_8088_listening": port_listening,
            "managed_by_supervisor": managed,
            "ai_pid": ai_pid,
            "ai_mode": resolved_mode,
            "desired_mode": desired_mode,
            "uptime_sec": round(uptime_sec, 1),
            "has_venv": caps["has_venv"],
            "has_onnx_model": caps["has_onnx_model"],
            "has_full_gpu": has_full_gpu,
            "can_stringing_ai": caps["can_stringing_ai"],
            "last_error": last_error,
            "exit_code": last_exit_code,
            "log_path": self.log_path,
            "port_8088_status": port_status,
        }

    def start(self, mode: str = "auto", wait_seconds: float = 2.8) -> Tuple[int, Dict[str, Any]]:
        req_mode = (mode or "auto").strip().lower()
        if req_mode not in ("auto", "full_gpu", "standalone_cpu"):
            req_mode = "auto"

        with self._op_lock:
            caps = self.inspect_capabilities(refresh=True)
            with self._state_lock:
                self._reconcile_child_locked()
                existing_proc = self.proc
                self.desired_mode = req_mode

            live = self.probe_ai_server(timeout=0.4)
            if live is not None or existing_proc is not None:
                st = self.get_status()
                st["already_running"] = True
                return 200, st

            if req_mode == "full_gpu" and not caps["has_full_gpu"]:
                err = "Modalità GPU completa richiede .venv (aiohttp + onnxruntime-directml) e il modello ONNX compilato."
                with self._state_lock:
                    self.last_error = err
                st = self.get_status()
                st["ok"] = False
                st["error"] = err
                return 400, st

            chosen_mode = "full_gpu" if (req_mode in ("auto", "full_gpu") and caps["has_full_gpu"]) else "standalone_cpu"
            if chosen_mode == "full_gpu":
                py_exe = str(caps["full_python"])
                script_path = os.path.join(self.repo_dir, "ai_upscaler_service.py")
            else:
                py_exe = str(caps["standalone_python"])
                script_path = os.path.join(self.repo_dir, "printer_telemetry.py")
                if not caps["standalone_script_exists"]:
                    err = f"Script server mancante: {script_path}"
                    with self._state_lock:
                        self.last_error = err
                    st = self.get_status()
                    st["ok"] = False
                    st["error"] = err
                    return 500, st

            os.makedirs(self.log_dir, exist_ok=True)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0

            with self._state_lock:
                self.last_error = ""
                self.last_exit_code = None

            try:
                with open(self.log_path, "wb") as log_f:
                    header = (
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"Starting {os.path.basename(script_path)} (mode={chosen_mode}) with {py_exe}\n"
                    ).encode("utf-8", errors="replace")
                    log_f.write(header)
                    log_f.flush()
                    proc = subprocess.Popen(
                        [py_exe, "-u", script_path],
                        cwd=self.repo_dir,
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                        env=env,
                        creationflags=creationflags,
                    )
            except Exception as exc:
                err = f"Impossibile avviare il server: {exc}"
                with self._state_lock:
                    self.last_error = err
                st = self.get_status()
                st["ok"] = False
                st["error"] = err
                return 500, st

            with self._state_lock:
                self.proc = proc
                self.started_at = time.time()
                self.active_mode = chosen_mode

            # Wait briefly until :8088 becomes reachable or process exits early
            deadline = time.monotonic() + max(0.2, float(wait_seconds))
            while time.monotonic() < deadline:
                rc = proc.poll()
                if rc is not None:
                    with self._state_lock:
                        self._reconcile_child_locked()
                    st = self.get_status()
                    st["ok"] = False
                    st["error"] = st.get("last_error") or f"Server terminato subito (exit code {rc})"
                    return 500, st
                if self.probe_ai_server(timeout=0.3) is not None:
                    break
                time.sleep(0.15)

            return 200, self.get_status()

    def stop(self, timeout: float = 3.5) -> Tuple[int, Dict[str, Any]]:
        with self._op_lock:
            # 1. Request clean cooperative HTTP shutdown first (works for both managed and external .bat servers!)
            try:
                req = urllib.request.Request(
                    f"http://{self.ai_host}:{self.ai_port}/server/stop",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=1.0):
                    pass
            except Exception:
                pass

            deadline = time.monotonic() + max(0.5, float(timeout))
            while time.monotonic() < deadline:
                with self._state_lock:
                    self._reconcile_child_locked()
                    alive_proc = self.proc is not None
                port_up = self.probe_ai_server(timeout=0.25) is not None
                if not alive_proc and not port_up:
                    break
                time.sleep(0.12)

            # 2. Force-terminate managed process if still alive after graceful wait
            with self._state_lock:
                proc = self.proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.2)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass

            with self._state_lock:
                if self.proc is not None and self.proc.poll() is not None:
                    self.proc = None
                self.started_at = 0.0
                self.active_mode = "stopped"
                self.last_error = ""

            st = self.get_status()
            if st["port_8088_listening"] and not st["managed_by_supervisor"]:
                st["warning"] = "La porta 8088 risponde ancora da un processo esterno avviato separatamente."
            return 200, st

    def restart(self, mode: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        req_mode = (mode or self.desired_mode or "auto").strip().lower()
        self.stop(timeout=3.0)
        return self.start(mode=req_mode)

    def read_logs(self, lines: int = 35) -> List[str]:
        n = max(1, min(200, int(lines)))
        if not os.path.isfile(self.log_path):
            return []
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.read().splitlines()
            return all_lines[-n:]
        except Exception:
            return []


def create_supervisor_server(
    supervisor: Optional[AIServerSupervisor] = None,
    host: str = CTRL_HOST,
    port: int = CTRL_PORT,
):
    sup = supervisor or AIServerSupervisor(ctrl_port=port)

    class Handler(BaseHTTPRequestHandler):
        def _send_cors(self, origin: Optional[str]) -> None:
            allow_origin = origin.strip() if (origin and _is_allowed_origin(origin)) else "*"
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")

        def _send_json(self, obj: dict, status: int = 200, origin: Optional[str] = None) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors(origin)
            self.end_headers()
            self.wfile.write(body)

        def _check_origin(self) -> Tuple[bool, Optional[str]]:
            origin = self.headers.get("Origin")
            if not _is_allowed_origin(origin):
                self._send_json({"ok": False, "error": "Forbidden origin"}, status=403, origin=origin)
                return False, origin
            return True, origin

        def _read_json_body(self) -> Dict[str, Any]:
            try:
                clen = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                clen = 0
            if clen <= 0 or clen > 65536:
                return {}
            raw = self.rfile.read(clen)
            if not raw:
                return {}
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}

        def do_OPTIONS(self) -> None:
            ok, origin = self._check_origin()
            if not ok:
                return
            self.send_response(204)
            self._send_cors(origin)
            self.end_headers()

        def do_GET(self) -> None:
            ok, origin = self._check_origin()
            if not ok:
                return
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path in ("/control/status", "/status"):
                refresh = (qs.get("refresh") or ["0"])[0] in ("1", "true")
                self._send_json(sup.get_status(refresh_caps=refresh), origin=origin)
            elif parsed.path == "/control/logs":
                try:
                    n = int((qs.get("lines") or ["35"])[0])
                except ValueError:
                    n = 35
                self._send_json(
                    {"ok": True, "log_path": sup.log_path, "lines": sup.read_logs(lines=n)},
                    origin=origin,
                )
            else:
                self._send_json({"ok": False, "error": "Not found"}, status=404, origin=origin)

        def do_POST(self) -> None:
            ok, origin = self._check_origin()
            if not ok:
                return
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json({"ok": False, "error": f"Invalid JSON: {exc}"}, status=400, origin=origin)
                return
            mode = body.get("mode") or (qs.get("mode") or [None])[0]

            if parsed.path == "/control/start":
                code, res = sup.start(mode=str(mode or "auto"))
                self._send_json(res, status=code, origin=origin)
            elif parsed.path == "/control/stop":
                code, res = sup.stop()
                self._send_json(res, status=code, origin=origin)
            elif parsed.path == "/control/restart":
                code, res = sup.restart(mode=mode)
                self._send_json(res, status=code, origin=origin)
            else:
                self._send_json({"ok": False, "error": "Not found"}, status=404, origin=origin)

        def log_message(self, format: str, *args) -> None:
            pass

    return _ReuseThreadingHTTPServer((host, port), Handler)


def probe_supervisor(host: str = CTRL_HOST, port: int = CTRL_PORT, timeout: float = 1.8) -> Optional[dict]:
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            pass
    except Exception:
        return None
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/control/status", timeout=timeout) as resp:
            if 200 <= int(resp.status) < 300:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def ensure_supervisor_running(host: str = CTRL_HOST, port: int = CTRL_PORT) -> bool:
    """Spawn background windowless supervisor (`pythonw server_manager.py`) if not already active."""
    if probe_supervisor(host, port) is not None:
        return True
    pyw = _resolve_pythonw_exe()
    script = os.path.abspath(__file__)
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen(
            [pyw, script],
            cwd=REPO_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception:
        return False

    deadline = time.monotonic() + 3.5
    while time.monotonic() < deadline:
        if probe_supervisor(host, port) is not None:
            return True
        time.sleep(0.15)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Creality Vision AI Local Server Supervisor (:8089)")
    parser.add_argument("--host", default=CTRL_HOST)
    parser.add_argument("--port", type=int, default=CTRL_PORT)
    parser.add_argument("--ensure-running", action="store_true", help="Start detached supervisor if offline and return")
    parser.add_argument("--status", action="store_true", help="Print live supervisor status JSON and exit")
    parser.add_argument("--stop-ai", action="store_true", help="Stop managed AI server via running supervisor")
    args = parser.parse_args()

    if args.ensure_running:
        ok = ensure_supervisor_running(args.host, args.port)
        print(json.dumps({"ok": ok, "supervisor_port": args.port}))
        return 0 if ok else 1

    if args.status:
        st = probe_supervisor(args.host, args.port)
        print(json.dumps(st or {"ok": False, "supervisor_online": False}, indent=2))
        return 0 if st else 1

    if args.stop_ai:
        try:
            req = urllib.request.Request(
                f"http://{args.host}:{args.port}/control/stop",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                print(resp.read().decode("utf-8"))
                return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1

    # Single-instance guard: exit cleanly if already serving on ctrl port
    if probe_supervisor(args.host, args.port) is not None:
        return 0

    sup = AIServerSupervisor(repo_dir=REPO_DIR, ctrl_port=args.port)
    sup.inspect_capabilities(refresh=True)
    srv = create_supervisor_server(supervisor=sup, host=args.host, port=args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sup.stop(timeout=2.0)
        except Exception:
            pass
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
