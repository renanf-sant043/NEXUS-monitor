#!/usr/bin/env python3
"""
NEXUS Monitor - agente de monitoramento (Windows, Linux e macOS).

O agente coleta as métricas do computador em segundo plano (uma vez a cada
2 s) e entrega o resultado pronto por uma API HTTP. Vários painéis podem
consultar o mesmo agente ao mesmo tempo sem sobrecarregar a máquina.

Uso rápido:
    python app.py                          # somente leitura, sem chave
    python app.py --key MINHA_CHAVE        # exige chave de acesso
    python app.py --stress                 # habilita o teste de estresse
                                           # (gera uma chave se não houver)

Endpoints:
    GET  /                 painel (index.html)
    GET  /api/ping         identifica o agente (sem chave)
    GET  /api/metrics      leitura mais recente
    GET  /api/history      últimas leituras (para o gráfico)
    POST /api/stress       inicia teste de estresse (só com --stress)
    POST /api/stress/stop  interrompe o teste de estresse
"""
from __future__ import annotations

import argparse
import hmac
import logging
import multiprocessing as mp
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.serving import make_server

VERSION = "2.0.0"
BASE_DIR = Path(__file__).resolve().parent
SAMPLE_INTERVAL = 2.0          # segundos entre leituras
HISTORY_SIZE = 450             # 450 leituras x 2 s = 15 minutos
DISK_ROOT = (os.environ.get("SystemDrive", "C:") + "\\") if os.name == "nt" else "/"

# ----------------------------------------------------------------------------
# Configuração (pode ser alterada por variáveis de ambiente ou por argumentos)
# ----------------------------------------------------------------------------
API_KEY = os.environ.get("NEXUS_API_KEY", "")
STRESS_ENABLED = os.environ.get("NEXUS_ENABLE_STRESS", "") == "1"
EXTRA_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.environ.get("NEXUS_ORIGINS", "").split(",")
    if o.strip()
}
ALLOWED_ORIGINS = {"https://renanf-sant043.github.io"} | EXTRA_ORIGINS

# Origens aceitas automaticamente: o próprio computador e redes privadas
# (10.x, 172.16-31.x, 192.168.x). Isso permite que o painel aberto em um PC
# consulte o agente de outro PC da mesma rede.
LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
PRIVATE_ORIGIN = re.compile(
    r"^https?://(10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?$"
)

app = Flask(__name__)
sampler: "Sampler | None" = None


# ----------------------------------------------------------------------------
# Funções de coleta
# ----------------------------------------------------------------------------
def safe(fn, default=None):
    """Executa fn() e devolve default se falhar (sensor ausente, sem permissão)."""
    try:
        return fn()
    except Exception:
        return default


def os_name() -> str:
    if os.name == "nt":
        build = getattr(sys, "getwindowsversion", lambda: None)()
        if build is not None:
            return "Windows 11" if build.build >= 22000 else f"Windows {platform.release()}"
    if sys.platform.startswith("linux"):
        release = safe(platform.freedesktop_os_release, {})
        if release.get("PRETTY_NAME"):
            return release["PRETTY_NAME"]
    return f"{platform.system()} {platform.release()}"


def cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or "Não informado"


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # não envia nenhum pacote
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def read_temperature() -> float | None:
    """Maior temperatura de CPU disponível (hoje só o Linux expõe isso no psutil)."""
    fn = getattr(psutil, "sensors_temperatures", None)
    if not fn:
        return None
    data = safe(fn, {}) or {}
    preferred, others = [], []
    for name, entries in data.items():
        for entry in entries:
            if entry.current and 0 < entry.current < 130:
                (preferred if name in ("coretemp", "k10temp", "cpu_thermal", "acpitz")
                 else others).append(entry.current)
    values = preferred or others
    return round(max(values), 1) if values else None


def read_gpu() -> dict | None:
    """Placa NVIDIA via nvidia-smi (se estiver instalado)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe,
             "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        line = result.stdout.strip().splitlines()[0]
        name, util, temp, used, total = [part.strip() for part in line.split(",")]
        return {
            "name": name,
            "util_percent": float(util),
            "temperature_c": float(temp),
            "mem_used_mb": float(used),
            "mem_total_mb": float(total),
        }
    except Exception:
        return None


def list_disks() -> list[dict]:
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype or part.fstype == "squashfs":
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        if usage.total == 0:
            continue
        disks.append({
            "mount": part.mountpoint,
            "percent": usage.percent,
            "used_gb": round(usage.used / 1024 ** 3, 1),
            "total_gb": round(usage.total / 1024 ** 3, 1),
        })
        if len(disks) >= 8:
            break
    return disks


def evaluate_health(cpu, ram, disk, temp) -> dict:
    """Nota de 0 a 100 com os motivos que a reduziram (em português)."""
    score, issues, critical = 100, [], False

    def check(value, warn, bad, penalty_warn, penalty_bad, text):
        nonlocal score, critical
        if value is None:
            return
        if value >= bad:
            score -= penalty_bad
            critical = True
            issues.append(f"{text} em nível crítico ({value:.0f})")
        elif value >= warn:
            score -= penalty_warn
            issues.append(f"{text} elevado ({value:.0f})")

    check(cpu, 75, 90, 8, 20, "Uso de CPU")
    check(ram, 80, 90, 10, 25, "Uso de memória")
    check(disk, 85, 95, 10, 25, "Ocupação do disco")
    check(temp, 75, 90, 10, 25, "Temperatura")
    score = max(score, 0)
    if critical:
        score = min(score, 65)  # qualquer item crítico impede nota "boa"
    label = ("Excelente" if score >= 90 else "Boa" if score >= 75
             else "Atenção" if score >= 50 else "Crítica")
    return {"score": score, "label": label, "issues": issues}


# ----------------------------------------------------------------------------
# Coleta em segundo plano
# ----------------------------------------------------------------------------
class Sampler(threading.Thread):
    """Lê as métricas a cada SAMPLE_INTERVAL e guarda a última leitura + histórico."""

    def __init__(self) -> None:
        super().__init__(name="nexus-sampler", daemon=True)
        self.lock = threading.Lock()
        self.latest: dict | None = None
        self.history: deque = deque(maxlen=HISTORY_SIZE)
        self.ncpu = psutil.cpu_count(logical=True) or 1
        self.static = {
            "hostname": socket.gethostname(),
            "os": os_name(),
            "cpu_name": cpu_name(),
            "logical_cores": self.ncpu,
            "physical_cores": psutil.cpu_count(logical=False),
            "ram_total_gb": round(psutil.virtual_memory().total / 1024 ** 3, 2),
            "ip": local_ip(),
        }
        self._prev_t = time.monotonic()
        self._prev_net = psutil.net_io_counters()
        self._prev_dio = safe(psutil.disk_io_counters)
        self._gpu = None
        self._tick = 0
        self._halt = threading.Event()
        psutil.cpu_percent(interval=None, percpu=True)  # "aquece" o contador

    # -- processos ----------------------------------------------------------
    def read_processes(self) -> tuple[list[dict], int]:
        groups: dict[str, list] = {}
        for proc in psutil.process_iter():
            try:
                with proc.oneshot():
                    if proc.pid == 0:
                        continue
                    name = proc.name()
                    cpu = proc.cpu_percent(None) / self.ncpu  # igual ao Gerenciador de Tarefas
                    ram = proc.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            g = groups.setdefault(name, [0.0, 0, 0])
            g[0] += cpu
            g[1] += ram
            g[2] += 1
        items = [
            {"name": n, "cpu_percent": round(min(v[0], 100), 1),
             "ram_mb": round(v[1] / 1048576, 1), "count": v[2]}
            for n, v in groups.items()
        ]
        top_cpu = sorted(items, key=lambda i: i["cpu_percent"], reverse=True)[:12]
        top_ram = sorted(items, key=lambda i: i["ram_mb"], reverse=True)[:12]
        merged = {i["name"]: i for i in top_cpu + top_ram}
        return list(merged.values()), len(items)

    # -- uma leitura completa ------------------------------------------------
    def sample(self, record: bool = True) -> None:
        now = time.monotonic()
        dt = max(now - self._prev_t, 0.001)
        self._prev_t = now
        self._tick += 1

        cores = psutil.cpu_percent(interval=None, percpu=True)
        cpu = round(sum(cores) / len(cores), 1) if cores else 0.0
        vm = psutil.virtual_memory()
        swap = safe(lambda: psutil.swap_memory().percent)
        disk = safe(lambda: psutil.disk_usage(DISK_ROOT))
        freq = safe(lambda: psutil.cpu_freq().current)
        temp = read_temperature()

        net = psutil.net_io_counters()
        rx = max(0.0, (net.bytes_recv - self._prev_net.bytes_recv) / dt / 1e6)
        tx = max(0.0, (net.bytes_sent - self._prev_net.bytes_sent) / dt / 1e6)
        self._prev_net = net

        dio = safe(psutil.disk_io_counters)
        read_mbs = write_mbs = None
        if dio and self._prev_dio:
            read_mbs = max(0.0, (dio.read_bytes - self._prev_dio.read_bytes) / dt / 1e6)
            write_mbs = max(0.0, (dio.write_bytes - self._prev_dio.write_bytes) / dt / 1e6)
        self._prev_dio = dio

        if self._tick % 3 == 1:  # nvidia-smi é lento: consulta a cada 3 leituras
            self._gpu = read_gpu()

        battery = safe(psutil.sensors_battery)
        processes, process_count = self.read_processes()
        disk_percent = disk.percent if disk else 0.0
        health = evaluate_health(cpu, vm.percent, disk_percent, temp)
        ts = round(time.time(), 2)

        data = {
            **self.static,
            "timestamp": ts,
            "interval": SAMPLE_INTERVAL,
            "cpu_percent": cpu,
            "cpu_cores_percent": cores,
            "cpu_freq_mhz": round(freq) if freq else None,
            "ram_percent": vm.percent,
            "ram_used_gb": round(vm.used / 1024 ** 3, 2),
            "swap_percent": swap,
            "disk_percent": disk_percent,
            "disk_used_gb": round(disk.used / 1024 ** 3, 2) if disk else 0,
            "disk_total_gb": round(disk.total / 1024 ** 3, 2) if disk else 0,
            "disks": list_disks(),
            "disk_read_mbs": round(read_mbs, 2) if read_mbs is not None else None,
            "disk_write_mbs": round(write_mbs, 2) if write_mbs is not None else None,
            "net_rx_mbs": round(rx, 3),
            "net_tx_mbs": round(tx, 3),
            "temperature_c": temp,
            "gpu": self._gpu,
            "battery": ({"percent": round(battery.percent), "plugged": bool(battery.power_plugged)}
                        if battery else None),
            "process_count": process_count,
            "processes": processes,
            "health": health,
        }
        with self.lock:
            self.latest = data
            if record:
                self.history.append({"t": ts, "cpu": cpu, "ram": vm.percent,
                                     "rx": round(rx, 3), "tx": round(tx, 3), "temp": temp})

    def run(self) -> None:
        while not self._halt.wait(SAMPLE_INTERVAL):
            try:
                self.sample()
            except Exception as exc:  # nunca deixa a coleta morrer
                print(f"[aviso] falha na leitura: {exc}")

    def halt(self) -> None:
        self._halt.set()

    def snapshot(self) -> dict:
        with self.lock:
            data = dict(self.latest or {})
        data["uptime_seconds"] = int(time.time() - psutil.boot_time())
        return data

    def history_points(self) -> list[dict]:
        with self.lock:
            return list(self.history)


# ----------------------------------------------------------------------------
# Teste de estresse (opcional, só com --stress)
# ----------------------------------------------------------------------------
STRESS_LEVELS = {"leve": 0.25, "medio": 0.5, "alto": 1.0}


def _burn(ends_at: float) -> None:
    """Consome 100% de um núcleo até ends_at, com prioridade baixa."""
    try:
        psutil.Process().nice(
            psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass
    x = 0
    while time.time() < ends_at:
        for _ in range(20000):
            x = (x * 31 + 7) % 1000003


class StressTest:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.procs: list[mp.Process] = []
        self.ends_at = 0.0

    def active(self) -> bool:
        return time.time() < self.ends_at and any(p.is_alive() for p in self.procs)

    def start(self, seconds: int, level: str) -> bool:
        with self.lock:
            if self.active():
                return False
            workers = max(1, round((psutil.cpu_count(logical=True) or 1) * STRESS_LEVELS[level]))
            self.ends_at = time.time() + seconds
            self.procs = [mp.Process(target=_burn, args=(self.ends_at,), daemon=True)
                          for _ in range(workers)]
            for p in self.procs:
                p.start()
            return True

    def stop(self) -> None:
        with self.lock:
            for p in self.procs:
                if p.is_alive():
                    p.terminate()
            self.procs = []
            self.ends_at = 0.0

    def status(self) -> dict:
        active = self.active()
        return {"enabled": STRESS_ENABLED, "active": active,
                "remaining": max(0, round(self.ends_at - time.time())) if active else 0}


stress = StressTest()


# ----------------------------------------------------------------------------
# Segurança: chave de acesso e CORS
# ----------------------------------------------------------------------------
def origin_allowed(origin: str) -> bool:
    if not origin:
        return False
    origin = origin.rstrip("/")
    return (origin in ALLOWED_ORIGINS
            or bool(LOCAL_ORIGIN.match(origin))
            or bool(PRIVATE_ORIGIN.match(origin))
            or origin == request.host_url.rstrip("/"))


def key_is_valid() -> bool:
    if not API_KEY:
        return True
    header = request.headers.get("Authorization", "")
    provided = header[7:] if header.lower().startswith("bearer ") else request.headers.get("X-API-Key", "")
    return hmac.compare_digest(provided.encode(), API_KEY.encode())


def protected(view):
    def wrapper(*args, **kwargs):
        if not key_is_valid():
            return jsonify(error="Chave de acesso ausente ou incorreta."), 401
        return view(*args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


@app.after_request
def add_headers(response):
    origin = request.headers.get("Origin", "")
    if origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key"
        response.headers["Access-Control-Max-Age"] = "600"
        # Chrome exige isto quando uma página consulta um IP de rede local.
        if request.headers.get("Access-Control-Request-Private-Network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ----------------------------------------------------------------------------
# Rotas
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    response = send_from_directory(str(BASE_DIR), "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.get("/api/ping")
def ping():
    return jsonify(app="nexus-monitor", version=VERSION,
                   auth_required=bool(API_KEY), stress_enabled=STRESS_ENABLED)


@app.get("/api/metrics")
@protected
def metrics():
    if sampler is None or sampler.latest is None:
        return jsonify(error="O agente ainda está iniciando."), 503
    data = sampler.snapshot()
    data["stress"] = stress.status()
    return jsonify(data)


@app.get("/api/history")
@protected
def history():
    return jsonify(interval=SAMPLE_INTERVAL, points=sampler.history_points() if sampler else [])


@app.post("/api/stress")
@protected
def stress_start():
    if not STRESS_ENABLED:
        return jsonify(error="Teste de estresse desativado. Inicie o agente com --stress."), 403
    body = request.get_json(silent=True) or {}
    level = body.get("level", "medio")
    if level not in STRESS_LEVELS:
        return jsonify(error="Intensidade inválida."), 400
    try:
        seconds = max(5, min(60, int(body.get("seconds", 15))))
    except (TypeError, ValueError):
        return jsonify(error="Duração inválida."), 400
    if not stress.start(seconds, level):
        return jsonify(error="Já existe um teste em andamento."), 409
    return jsonify(stress.status())


@app.post("/api/stress/stop")
@protected
def stress_stop():
    stress.stop()
    return jsonify(stress.status())


# ----------------------------------------------------------------------------
# Inicialização
# ----------------------------------------------------------------------------
def start_sampler() -> Sampler:
    global sampler
    sampler = Sampler()
    sampler.sample(record=False)   # primeira leitura serve só de referência
    time.sleep(0.6)
    sampler.sample()
    sampler.start()
    return sampler


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NEXUS Monitor - agente")
    p.add_argument("--host", default="0.0.0.0", help="endereço de escuta (padrão: 0.0.0.0)")
    p.add_argument("--port", type=int, default=int(os.environ.get("NEXUS_PORT", 5000)))
    p.add_argument("--key", default=None, help="chave de acesso exigida pela API")
    p.add_argument("--stress", action="store_true", help="habilita o teste de estresse")
    p.add_argument("--origins", default="", help="origens extras permitidas (separadas por vírgula)")
    return p.parse_args()


def main() -> None:
    global API_KEY, STRESS_ENABLED
    mp.freeze_support()
    args = parse_args()
    if args.key:
        API_KEY = args.key
    if args.stress:
        STRESS_ENABLED = True
    for origin in filter(None, (o.strip().rstrip("/") for o in args.origins.split(","))):
        ALLOWED_ORIGINS.add(origin)

    generated = False
    if STRESS_ENABLED and not API_KEY:
        API_KEY, generated = secrets.token_urlsafe(8), True  # estresse nunca fica sem chave

    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # sem log a cada leitura
    start_sampler()
    ip = sampler.static["ip"]

    print("=" * 56)
    print(f" NEXUS MONITOR - AGENTE v{VERSION}")
    print(f" Painel neste PC : http://127.0.0.1:{args.port}")
    print(f" Painel na rede  : http://{ip}:{args.port}")
    print(f" API             : http://{ip}:{args.port}/api/metrics")
    print(f" Chave de acesso : {API_KEY if API_KEY else 'DESATIVADA (somente leitura, rede local)'}"
          + ("  (gerada agora)" if generated else ""))
    print(f" Teste de estresse: {'ATIVADO' if STRESS_ENABLED else 'desativado'}")
    print(" Não encaminhe a porta para a internet. Ctrl+C para encerrar.")
    print("=" * 56)

    try:
        server = make_server(args.host, args.port, app, threaded=True)
    except OSError as exc:
        print(f"\nNão foi possível abrir a porta {args.port}: {exc}")
        print("Feche o outro programa que a usa ou rode com --port 5001.")
        sys.exit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stress.stop()
        sampler.halt()
        print("\nAgente encerrado.")


if __name__ == "__main__":
    main()
