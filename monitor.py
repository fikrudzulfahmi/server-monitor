#!/usr/bin/env python3
"""Server monitor dashboard - stdlib only. Reads /proc & /sys."""
import glob, json, os, socket, time, threading, fcntl, struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("MON_HOST", "127.0.0.1")
PORT = int(os.environ.get("MON_PORT", "8090"))
USER = os.environ.get("MON_USER", "")
PASS = os.environ.get("MON_PASS", "")
# token tetap untuk widget Android (tanpa login/cookie)
WIDGET_TOKEN = os.environ.get("MON_WIDGET_TOKEN", "")
# kontrol daya: password khusus (default = password panel) + jeda sebelum eksekusi
POWER_PASS = os.environ.get("MON_POWER_PASS", "") or PASS
# tombol shutdown dimatikan secara default (aktifkan dengan MON_POWER_ENABLED=1)
POWER_ENABLED = os.environ.get("MON_POWER_ENABLED", "0") == "1"
POWER_DELAY = int(os.environ.get("MON_POWER_DELAY", "30"))
# root filesystem host saat dijalankan di container (default: root container sendiri)
PROC = os.environ.get("PROC_ROOT", "/proc").rstrip("/")
SYS = os.environ.get("SYS_ROOT", "/sys").rstrip("/")
# prefix untuk statvfs (container: /host/root supaya ukuran disk host yang terbaca)
FS_PREFIX = os.environ.get("FS_ROOT_PREFIX", "").rstrip("/")
# tabel mount: di container wajib /host/proc/1/mounts (PID 1 host)
MOUNTS_FILE = os.environ.get("MOUNTS_FILE", "")
# path log CUPS (bisa dikosongkan bila CUPS berjalan di container lain)
ACCESS_LOG = os.environ.get("CUPS_ACCESS_LOG", "/var/log/cups/access_log")
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------- CPU sampler (background) ----------
_cpu = {"total": 0.0, "per": []}
def _read_stat():
    rows = []
    with open(PROC + "/stat") as f:
        for line in f:
            if line.startswith("cpu"):
                p = line.split(); v = list(map(int, p[1:]))
                idle = v[3] + (v[4] if len(v) > 4 else 0)
                rows.append((sum(v), idle))
    return rows
def _cpu_loop():
    prev = _read_stat()
    while True:
        time.sleep(1)
        cur = _read_stat(); res = []
        for (t1, i1), (t2, i2) in zip(prev, cur):
            dt = t2 - t1
            res.append(round(100 * (dt - (i2 - i1)) / dt, 1) if dt else 0.0)
        _cpu["total"], _cpu["per"] = res[0], res[1:]
        prev = cur
threading.Thread(target=_cpu_loop, daemon=True).start()

# ---------- Net sampler ----------
_net = {"rx": 0.0, "tx": 0.0, "rx_total": 0, "tx_total": 0}
def _read_net():
    rx = tx = 0
    with open(PROC + "/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1); name = name.strip()
            if name == "lo" or name.startswith(("docker", "veth", "br-")): continue
            d = data.split(); rx += int(d[0]); tx += int(d[8])
    return rx, tx
def _net_loop():
    prx, ptx = _read_net()
    while True:
        time.sleep(1)
        rx, tx = _read_net()
        _net.update(rx=rx - prx, tx=tx - ptx, rx_total=rx, tx_total=tx)
        prx, ptx = rx, tx
threading.Thread(target=_net_loop, daemon=True).start()

def mem():
    m = {}
    with open(PROC + "/meminfo") as f:
        for line in f:
            k, v = line.split(":"); m[k] = int(v.split()[0]) * 1024
    total, avail = m["MemTotal"], m.get("MemAvailable", m["MemFree"])
    st, sf = m.get("SwapTotal", 0), m.get("SwapFree", 0)
    return {"total": total, "used": total - avail, "free": avail,
            "percent": round(100 * (total - avail) / total, 1),
            "swap_total": st, "swap_used": st - sf,
            "swap_percent": round(100 * (st - sf) / st, 1) if st else 0}

def disks():
    out, seen = [], set()
    skip = ("tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2")
    with open(MOUNTS_FILE or (PROC + "/mounts")) as f:
        for line in f:
            dev, mnt, fs = line.split()[:3]
            if fs in skip or not dev.startswith("/dev/") or dev in seen: continue
            if mnt.startswith(("/snap", "/boot/efi")): continue
            seen.add(dev)
            try: s = os.statvfs(FS_PREFIX + mnt)
            except OSError: continue
            total = s.f_blocks * s.f_frsize; free = s.f_bavail * s.f_frsize
            used = total - s.f_bfree * s.f_frsize
            if total == 0: continue
            out.append({"mount": mnt, "device": dev, "fs": fs, "total": total, "used": used,
                        "free": free, "percent": round(100 * used / (used + free), 1)})
    return out

def temps():
    res = []
    for hw in glob.glob(SYS + "/class/hwmon/hwmon*"):
        try: name = open(hw + "/name").read().strip()
        except OSError: name = "?"
        for t in sorted(glob.glob(hw + "/temp*_input")):
            try: val = int(open(t).read()) / 1000
            except (OSError, ValueError): continue
            lbl_f = t.replace("_input", "_label")
            label = open(lbl_f).read().strip() if os.path.exists(lbl_f) else name
            crit_f = t.replace("_input", "_crit")
            crit = int(open(crit_f).read()) / 1000 if os.path.exists(crit_f) else None
            res.append({"sensor": name, "label": label, "temp": val, "crit": crit})
    for z in glob.glob(SYS + "/class/thermal/thermal_zone*"):
        try:
            res.append({"sensor": open(z + "/type").read().strip(), "label": os.path.basename(z),
                        "temp": int(open(z + "/temp").read()) / 1000, "crit": None})
        except (OSError, ValueError): pass
    return res

def ips():
    out = []
    for iface in sorted(os.listdir(SYS + "/class/net")):
        if iface == "lo" or iface.startswith(("veth", "br-", "docker", "virbr", "tun", "tap")): continue
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915,
                 struct.pack("256s", iface[:15].encode()))[20:24])
            out.append({"iface": iface, "ip": ip})
        except OSError: pass
        finally: s.close()
    return out

def cpu_model():
    try:
        for line in open(PROC + "/cpuinfo"):
            if line.startswith("model name"): return line.split(":", 1)[1].strip()
    except OSError: pass
    return "?"

def top_procs(n=6):
    procs = []
    pagesize = os.sysconf("SC_PAGE_SIZE")
    for pid in filter(str.isdigit, os.listdir(PROC)):
        try:
            with open(f"{PROC}/{pid}/statm") as f: rss = int(f.read().split()[1]) * pagesize
            with open(f"{PROC}/{pid}/comm") as f: name = f.read().strip()
            procs.append({"pid": int(pid), "name": name, "rss": rss})
        except (OSError, ValueError, IndexError): pass
    return sorted(procs, key=lambda p: -p["rss"])[:n]

# ---------- CPU per process (background sampler) ----------
_CLK = os.sysconf("SC_CLK_TCK")
_pcpu = {}      # pid -> persen CPU dalam interval sampling

def _proc_ticks():
    out = {}
    for pid in filter(str.isdigit, os.listdir(PROC)):
        try:
            with open(f"{PROC}/{pid}/stat", "rb") as f: s = f.read().decode(errors="ignore")
        except OSError: continue
        i = s.rfind(")")
        if i < 0: continue
        p = s[i + 2:].split()
        try: out[int(pid)] = int(p[11]) + int(p[12])   # utime + stime
        except (IndexError, ValueError): continue
    return out

def _pcpu_loop():
    prev, t0 = _proc_ticks(), time.time()
    while True:
        time.sleep(2)
        cur, t1 = _proc_ticks(), time.time()
        dt = t1 - t0 or 1
        res = {}
        for pid, v in cur.items():
            old = prev.get(pid)
            if old is None: continue
            pct = 100.0 * (v - old) / (_CLK * dt)
            if pct >= 0.1: res[pid] = round(pct, 1)
        with _lock_pcpu:
            _pcpu.clear(); _pcpu.update(res)
        prev, t0 = cur, t1

import threading as _thr
_lock_pcpu = _thr.Lock()
_thr.Thread(target=_pcpu_loop, daemon=True).start()

def top_procs_cpu(n=6):
    with _lock_pcpu:
        items = sorted(_pcpu.items(), key=lambda kv: -kv[1])[:n]
    out = []
    for pid, pct in items:
        try:
            with open(f"{PROC}/{pid}/comm") as f: name = f.read().strip()
        except OSError:
            name = "?"
        out.append({"pid": pid, "name": name, "cpu": pct})
    return out

import re as _re, datetime as _dt, subprocess as _sp

_LOG_PAT = _re.compile(
    r'(\S+) - (\S+) \[([^\]]+)\] "POST /printers/(\S+?) HTTP[^"]*" (\d+) (\d+) ([\w-]+) (\S+)')

# ---- CUPS admin commands (pause/resume/cancel) ----
CUPSENABLE, CUPSDISABLE, CANCEL = "/usr/sbin/cupsenable", "/usr/sbin/cupsdisable", "/usr/bin/cancel"
_PRINTER_RE = _re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_JOB_RE = _re.compile(r"^\d{1,10}$")

def _cups_admin(argv, timeout=20):
    """Run a CUPS admin command as root via sudo -n. Returns (ok, output)."""
    if argv[0] not in (CUPSENABLE, CUPSDISABLE, CANCEL):
        return False, "perintah tidak diizinkan"
    mode = os.environ.get("CUPS_ADMIN_SUDO", "auto")
    attempts = [argv] if (os.geteuid() == 0 or mode == "never") else [argv, ["sudo", "-n"] + argv]
    if mode == "always":
        attempts = [["sudo", "-n"] + argv]
    p = None
    for full in attempts:
        try:
            p = _sp.run(full, capture_output=True, text=True, timeout=timeout)
        except _sp.TimeoutExpired:
            return False, "Perintah timeout."
        except OSError as e:
            if full is attempts[-1]:
                return False, f"Gagal menjalankan perintah: {e}"
            continue
        if p.returncode == 0:
            break
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        if "password is required" in out or "not allowed" in out.lower():
            return False, ("Izin sudo belum diatur. Di server jalankan sekali:\n"
                           "sudo bash ~/server-monitor/setup-print-control.sh")
        return False, out or "Perintah gagal."
    return True, out or "OK"

def _known_printers():
    try:
        p = _sp.run(["lpstat", "-p", "-a"], capture_output=True, text=True, timeout=10)
        return [l.split()[1] for l in p.stdout.splitlines() if l.startswith("printer ")]
    except Exception:
        return []

def print_action(action, printer="", job=""):
    """pause | resume | cancel | cancel_all. Returns (ok, message)."""
    printer = (printer or "").strip()
    if printer and not _PRINTER_RE.match(printer):
        return False, "Nama printer tidak valid."
    if printer and printer not in _known_printers():
        return False, f"Printer '{printer}' tidak dikenal."
    if action in ("pause", "resume", "cancel_all") and not printer:
        return False, "Printer harus disebutkan."

    if action == "pause":
        ok, out = _cups_admin([CUPSDISABLE, "-r", "Dijeda dari dashboard", printer])
        return (True, f"Antrian {printer} dijeda.") if ok else (False, out)

    if action == "resume":
        ok, out = _cups_admin([CUPSENABLE, printer])
        return (True, f"Antrian {printer} dilanjutkan.") if ok else (False, out)

    if action == "cancel":
        if not _JOB_RE.match((job or "").strip()):
            return False, "Nomor job tidak valid."
        if not printer:
            return False, "Printer harus disebutkan."
        ok, out = _cups_admin([CANCEL, f"{printer}-{job.strip()}"])
        return (True, f"Job #{job} dibatalkan.") if ok else (False, out)

    if action == "cancel_all":
        ok, out = _cups_admin([CANCEL, "-a", printer])
        return (True, f"Semua job di {printer} dibatalkan.") if ok else (False, out)

    return False, "Aksi tidak dikenal."


def _parse_log_ts(s):
    """Parse CUPS log timestamp like 15/Oct/2026:03:33:06 +0000"""
    try: return int(_dt.datetime.strptime(s, "%d/%b/%Y:%H:%M:%S %z").timestamp())
    except Exception: return 0

def print_jobs(limit=50):
    """Antrian aktif dari lpstat + riwayat dari access_log."""
    # ---- antrian aktif ----
    queue = []
    try:
        import subprocess
        out = subprocess.check_output(["lpstat", "-o"], stderr=subprocess.DEVNULL, text=True)
        for line in out.splitlines():
            p = line.split()
            if not p: continue
            job_id = p[0]; size = int(p[2]) if len(p) > 2 else 0
            ts_str = " ".join(p[3:7]) if len(p) >= 7 else ""
            try: ts = int(_dt.datetime.strptime(ts_str, "%a %b %d %H:%M:%S %Y").timestamp())
            except Exception: ts = 0
            parts = job_id.split("-")
            printer = "-".join(parts[:-1]); jid = parts[-1] if len(parts) > 1 else "?"
            queue.append({"id": jid, "printer": printer, "size_b": size,
                          "state": "Antrian", "state_code": "queue",
                          "user": "?", "client_ip": "?",
                          "created": _dt.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "-",
                          "done": "-", "created_ts": ts, "done_ts": 0})
    except Exception: pass

    # ---- riwayat dari access_log ----
    history = []
    try:
        with open(ACCESS_LOG, errors="ignore") as f:
            lines = f.readlines()
        for line in lines[-500:]:
            m = _LOG_PAT.match(line.strip())
            if not m: continue
            client, user, ts_s, printer, http_code, size_b, op, result = m.groups()
            if op not in ("Print-Job", "Send-Document"): continue
            ts = _parse_log_ts(ts_s)
            ok = result == "successful-ok" and http_code == "200"
            history.append({
                "id": "-", "printer": printer,
                "size_b": int(size_b), "size_kb": round(int(size_b)/1024, 1),
                "state": "Selesai" if ok else "Gagal",
                "state_code": "done" if ok else "error",
                "user": user if user != "-" else "?",
                "client_ip": client,
                "created": _dt.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "-",
                "done": _dt.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "-",
                "created_ts": ts, "done_ts": ts,
            })
        history = history[-limit:]
        history.reverse()      # terbaru di atas
    except Exception: pass

    # status printer
    printers = []
    try:
        out = subprocess.check_output(["lpstat", "-p", "-a"], stderr=subprocess.DEVNULL, text=True)
        for line in out.splitlines():
            if line.startswith("printer "):
                parts = line.split(); name = parts[1]
                paused = "disabled" in line
                idle = "idle" in line; printing = "printing" in line
                if paused:
                    state, code = "Dijeda", "paused"
                elif printing:
                    state, code = "Mencetak", "printing"
                elif idle:
                    state, code = "Siap", "ok"
                else:
                    state, code = "Tidak aktif", "off"
                printers.append({"name": name, "state": state, "state_code": code,
                                 "paused": paused, "printing": printing})
        # printer yang tidak menerima job baru (lpstat -a)
        for line in out.splitlines():
            if line.startswith("printer ") and "not accepting" in line:
                n = line.split()[1]
                for p in printers:
                    if p["name"] == n and not p["paused"]:
                        p["state"], p["state_code"] = "Berhenti terima job", "off"
    except Exception: pass

    return {"queue": queue, "history": history, "printers": printers}

def stats():
    up = float(open(PROC + "/uptime").read().split()[0])
    return {"hostname": socket.gethostname(), "time": int(time.time()),
            "uptime": up, "load": os.getloadavg(), "cpu_model": cpu_model(),
            "cores": os.cpu_count(), "cpu": _cpu["total"], "cpu_per": _cpu["per"],
            "mem": mem(), "disks": disks(), "temps": temps(), "ips": ips(),
            "net": dict(_net), "procs": top_procs(), "procs_cpu": top_procs_cpu(),
            "power": power_state(), "power_delay": POWER_DELAY, "power_enabled": POWER_ENABLED,
            "os": next((l.split("=", 1)[1].strip().strip('"') for l in open("/etc/os-release")
                        if l.startswith("PRETTY_NAME")), "Linux"),
            "kernel": os.uname().release}

# ---------- Session auth ----------
import hmac, secrets, urllib.parse
from http.cookies import SimpleCookie
SESSION_TTL = 12 * 3600
_sessions = {}          # token -> expiry
_fails = {}             # ip -> [count, first_ts]
_lock = threading.Lock()

def _client_ip(h):
    return (h.headers.get("CF-Connecting-IP") or h.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or h.client_address[0])

def _blocked(ip):
    with _lock:
        c = _fails.get(ip)
        if c and time.time() - c[1] > 900: _fails.pop(ip, None); return False
        return bool(c and c[0] >= 5)

def _fail(ip):
    with _lock:
        c = _fails.setdefault(ip, [0, time.time()]); c[0] += 1

# ---------- Kontrol daya (shutdown / reboot) ----------
import subprocess as _sp
SYSTEMCTL = "/usr/bin/systemctl"
_power = {"action": None, "deadline": 0, "ip": "", "error": ""}
_power_lock = threading.Lock()

def _can_power():
    """Cek apakah sudo NOPASSWD untuk systemctl tersedia (tanpa mengubah apa pun)."""
    try:
        r = _sp.run(["sudo", "-n", SYSTEMCTL, "show", "-p", "Version"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

def _run_power(action):
    """Hentikan NVR dengan rapi (mp4 ditutup), lalu matikan/reboot sistem."""
    try:
        _sp.run(["systemctl", "--user", "stop", "nvr.service"],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=25)
    except Exception:
        pass
    p = _sp.run(["sudo", "-n", SYSTEMCTL, action, "--no-block"],
                capture_output=True, text=True, timeout=30)
    return p.returncode, (p.stderr or p.stdout or "").strip()

def _power_worker(target, label, delay):
    time.sleep(delay)
    with _power_lock:
        if _power["action"] != label:       # dibatalkan saat hitungan mundur
            return
    rc, err = _run_power(target)
    if rc != 0:
        print(f"[power] gagal {label}: {err or rc}", flush=True)
        with _power_lock:
            _power.update({"action": None, "deadline": 0, "error": err or f"exit {rc}"})

def power_state():
    with _power_lock:
        left = max(0, int(_power["deadline"] - time.time())) if _power["action"] else 0
        return {"action": _power["action"], "seconds_left": left, "error": _power["error"]}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["mon_sess"].value if "mon_sess" in c else None

    def _authed(self):
        if not USER: return True
        t = self._token()
        with _lock:
            now = time.time()
            for k in [k for k, v in _sessions.items() if v < now]: _sessions.pop(k)
            return bool(t and t in _sessions)

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto") == "https" or "CF-Ray" in self.headers

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or []): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, "_head", False): self.wfile.write(body)

    def do_HEAD(self):
        self._head = True
        try: self.do_GET()
        finally: self._head = False

    def _redirect(self, loc, extra=None):
        self._send(303, b"", "text/plain", [("Location", loc)] + (extra or []))

    def _file(self, name):
        with open(os.path.join(HERE, name), "rb") as f:
            self._send(200, f.read(), "text/html; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/login":
            if self._authed(): return self._redirect("/")
            return self._file("login.html")
        if path == "/logout":
            t = self._token()
            with _lock: _sessions.pop(t, None)
            return self._redirect("/login", [("Set-Cookie", "mon_sess=; Path=/; Max-Age=0")])
        _STATIC = {"manifest.json": "application/json", "sw.js": "application/javascript",
                   "icon-192.png": "image/png", "icon-512.png": "image/png"}
        if path.lstrip("/") in _STATIC:
            name = path.lstrip("/")
            try:
                with open(os.path.join(HERE, name), "rb") as f:
                    return self._send(200, f.read(), _STATIC[name])
            except FileNotFoundError:
                return self._send(404, b"not found", "text/plain")
        if path in ("/api/widget", "/api/widget.json", "/api/widget.txt"):
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            tok = (qs.get("token") or [""])[0] or self.headers.get("X-Mon-Token", "")
            ok = bool(WIDGET_TOKEN) and hmac.compare_digest(tok.encode(), WIDGET_TOKEN.encode())
            if not ok: return self._send(401, b'{"error":"unauthorized"}', "application/json")
            w = widget_stats()
            if path.endswith(".txt"):
                body = (f"CPU {w['cpu']}% | RAM {w['ram_used_gb']}/{w['ram_total_gb']}G "
                        f"({w['ram_pct']}%) | DISK {w['disk_pct']}% | "
                        f"{('%.0fC' % w['temp']) if w['temp'] is not None else 'n/a'} | "
                        f"UP {w['uptime']//86400}d{(w['uptime']%86400)//3600}h").encode()
                return self._send(200, body, "text/plain; charset=utf-8")
            return self._send(200, json.dumps(w).encode(), "application/json")
        if not self._authed():
            if path.startswith("/api/"): return self._send(401, b'{"error":"unauthorized"}', "application/json")
            return self._redirect("/login")
        if path == "/api/stats":
            self._send(200, json.dumps(stats()).encode(), "application/json")
        elif path == "/api/print":
            self._send(200, json.dumps(print_jobs()).encode(), "application/json")
        elif path == "/api/speedtest":
            with _speedtest_lock:
                self._send(200, json.dumps(dict(_speedtest_result)).encode(), "application/json")
        elif path in ("/", "/index.html"):
            self._file("index.html")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/speedtest/run":
            if not self._authed():
                return self._send(401, b'{"error":"unauthorized"}', "application/json")
            with _speedtest_lock:
                if _speedtest_result["running"]:
                    return self._send(409, b'{"ok":false,"error":"Speedtest sedang berjalan"}', "application/json")
            threading.Thread(target=_run_speedtest, daemon=True).start()
            return self._send(200, b'{"ok":true,"message":"Speedtest dimulai"}', "application/json")
        if path == "/api/system/power":
            if not self._authed():
                return self._send(401, b'{"error":"unauthorized"}', "application/json")
            n = min(int(self.headers.get("Content-Length", 0) or 0), 2048)
            raw = self.rfile.read(n).decode(errors="ignore") if n else ""
            try: data = json.loads(raw) if raw else {}
            except ValueError: data = {}
            action = str(data.get("action", "")).strip().lower()
            ip = _client_ip(self)
            if not POWER_ENABLED:
                return self._send(503, b'{"ok":false,"error":"Kontrol daya dinonaktifkan"}', "application/json")
            if action == "cancel":
                with _power_lock:
                    was = _power["action"]
                    _power.update({"action": None, "deadline": 0, "error": ""})
                print(f"[power] dibatalkan oleh {ip} ({was or '-'})", flush=True)
                return self._send(200, json.dumps({"ok": True, "cancelled": bool(was)}).encode(),
                                  "application/json")
            if action in ("shutdown", "poweroff"): target, action = "poweroff", "shutdown"
            elif action == "reboot": target = "reboot"
            else:
                return self._send(400, b'{"ok":false,"error":"Aksi tidak dikenal"}', "application/json")
            if _blocked(ip):
                return self._send(429, b'{"ok":false,"error":"Terlalu banyak percobaan. Coba lagi 15 menit."}',
                                  "application/json")
            if not POWER_PASS:
                return self._send(503, b'{"ok":false,"error":"Password panel belum diset, kontrol daya dinonaktifkan"}',
                                  "application/json")
            pw = str(data.get("password", ""))
            if not hmac.compare_digest(pw.encode(), POWER_PASS.encode()):
                _fail(ip); time.sleep(1)
                print(f"[power] password salah dari {ip}", flush=True)
                return self._send(403, b'{"ok":false,"error":"Password salah"}', "application/json")
            with _lock: _fails.pop(ip, None)
            if not _can_power():
                return self._send(503, b'{"ok":false,"error":"Server belum mengizinkan kontrol daya (sudo systemctl)"}',
                                  "application/json")
            with _power_lock:
                if _power["action"]:
                    return self._send(409, json.dumps({"ok": False, "error": "Sudah ada jadwal " + _power["action"]}).encode(),
                                      "application/json")
                _power.update({"action": action, "deadline": time.time() + POWER_DELAY, "ip": ip, "error": ""})
            print(f"[power] {action} dijadwalkan oleh {ip} dalam {POWER_DELAY}s", flush=True)
            threading.Thread(target=_power_worker, args=(target, action, POWER_DELAY), daemon=True).start()
            return self._send(200, json.dumps({"ok": True, "action": action, "delay": POWER_DELAY}).encode(),
                              "application/json")
        if path == "/api/print/action":
            if not self._authed():
                return self._send(401, b'{"error":"unauthorized"}', "application/json")
            n = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
            raw = self.rfile.read(n).decode(errors="ignore")
            try: data = json.loads(raw) if raw else {}
            except ValueError: data = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            ok, msg = print_action(str(data.get("action", "")), str(data.get("printer", "")),
                                   str(data.get("job", "")))
            return self._send(200 if ok else 400,
                              json.dumps({"ok": ok, "message": msg}).encode(), "application/json")
        if path != "/login": return self._send(404, b"not found", "text/plain")
        ip = _client_ip(self)
        if _blocked(ip):
            return self._send(429, b'{"ok":false,"error":"Terlalu banyak percobaan. Coba lagi 15 menit."}', "application/json")
        n = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
        raw = self.rfile.read(n).decode(errors="ignore")
        try: data = json.loads(raw)
        except ValueError: data = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        u, pw = str(data.get("username", "")), str(data.get("password", ""))
        if hmac.compare_digest(u.encode(), USER.encode()) & hmac.compare_digest(pw.encode(), PASS.encode()):
            with _lock: _fails.pop(ip, None)
            t = secrets.token_urlsafe(32)
            with _lock: _sessions[t] = time.time() + SESSION_TTL
            ck = f"mon_sess={t}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Strict" + ("; Secure" if self._secure() else "")
            return self._send(200, b'{"ok":true}', "application/json", [("Set-Cookie", ck)])
        _fail(ip); time.sleep(1)
        self._send(401, b'{"ok":false,"error":"Username atau password salah"}', "application/json")

# ---------- Speedtest (one-at-a-time) ----------
_speedtest_lock = threading.Lock()
_speedtest_result = {"running": False, "data": None, "error": None, "ts": 0}

def _run_speedtest():
    """Run speedtest in background thread, store result."""
    import subprocess as _sub
    with _speedtest_lock:
        if _speedtest_result["running"]:
            return  # already running
        _speedtest_result["running"] = True
        _speedtest_result["error"] = None
    try:
        p = _sub.run(["/snap/bin/speedtest", "--format=json", "--accept-license", "--accept-gdpr"],
                     capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            with _speedtest_lock:
                _speedtest_result["error"] = (p.stderr or p.stdout or "speedtest failed").strip()[:500]
        else:
            raw = json.loads(p.stdout)
            dl_mbps = round(raw["download"]["bandwidth"] * 8 / 1_000_000, 2)
            ul_mbps = round(raw["upload"]["bandwidth"] * 8 / 1_000_000, 2)
            with _speedtest_lock:
                _speedtest_result["data"] = {
                    "download_mbps": dl_mbps,
                    "upload_mbps": ul_mbps,
                    "ping_ms": raw["ping"]["latency"],
                    "jitter_ms": raw["ping"]["jitter"],
                    "packet_loss": raw.get("packetLoss", None),
                    "isp": raw.get("isp", ""),
                    "server": raw["server"]["name"] + ", " + raw["server"]["country"],
                    "server_host": raw["server"]["host"],
                    "result_url": raw.get("result", {}).get("url", ""),
                    "external_ip": raw.get("interface", {}).get("externalIp", ""),
                }
    except _sub.TimeoutExpired:
        with _speedtest_lock:
            _speedtest_result["error"] = "Speedtest timeout (>120s)"
    except Exception as e:
        with _speedtest_lock:
            _speedtest_result["error"] = str(e)[:500]
    finally:
        with _speedtest_lock:
            _speedtest_result["running"] = False
            _speedtest_result["ts"] = int(time.time())

def widget_stats():
    """Payload ringkas untuk widget Android."""
    s = stats()
    m = s["mem"]
    root = next((d for d in s["disks"] if d["mount"] == "/"), s["disks"][0] if s["disks"] else None)
    tmax = max([t["temp"] for t in s["temps"]], default=None)
    gb = lambda b: round(b / 1073741824, 1)
    return {
        "host": s["hostname"], "time": s["time"], "uptime": int(s["uptime"]),
        "cpu": s["cpu"], "cores": s["cores"], "load1": round(s["load"][0], 2),
        "ram_pct": m["percent"], "ram_used_gb": gb(m["used"]), "ram_total_gb": gb(m["total"]),
        "swap_pct": m["swap_percent"],
        "disk_pct": root["percent"] if root else 0,
        "disk_used_gb": gb(root["used"]) if root else 0,
        "disk_free_gb": gb(root["free"]) if root else 0,
        "temp": round(tmax, 1) if tmax is not None else None,
        "net_rx_kbs": round(s["net"]["rx"] / 1024, 1), "net_tx_kbs": round(s["net"]["tx"] / 1024, 1),
        "ip": (s["ips"][0]["ip"] if s["ips"] else None),
    }

if __name__ == "__main__":
    print(f"Server monitor on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
