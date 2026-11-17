from flask import Flask, jsonify, send_from_directory, request
from werkzeug.serving import make_server
from pathlib import Path
import psutil
import platform
import socket
import time
import os

BASE_DIR = Path(__file__).resolve().parent
HOST = "0.0.0.0"
PORT = 5000

# CORS permite que o painel web faça requisições à API.
# Isso NÃO contorna bloqueios HTTPS -> HTTP ou regras de rede privada do navegador.
app = Flask(__name__)
ALLOWED_ORIGINS = {
    "https://renanf-sant043.github.io",
    "http://localhost",
    "http://127.0.0.1",
}
# O painel do GitHub Pages usa HTTPS; permita origens HTTPS para facilitar
# o teste inicial. Para um uso permanente, restrinja a origem ao seu domínio.
@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin.startswith("https://") or origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/api/metrics", methods=["OPTIONS"])
def metrics_preflight():
    return ("", 204)

psutil.cpu_percent(interval=None)
previous_net = psutil.net_io_counters()
previous_time = time.monotonic()
process_cache = {}


@app.get("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.get("/api/metrics")
def metrics():
    global previous_net, previous_time

    cpu = psutil.cpu_percent(interval=0.2)
    memory = psutil.virtual_memory()

    drive = os.environ.get("SystemDrive", "C:") + "\\"
    disk = psutil.disk_usage(drive)

    now = time.monotonic()
    net = psutil.net_io_counters()
    elapsed = max(now - previous_time, 0.001)

    rx_mbps = max(
        0,
        (net.bytes_recv - previous_net.bytes_recv)
        / elapsed / 1_000_000
    )

    previous_net = net
    previous_time = now

    processes = []
    active_pids = set()

    for proc in psutil.process_iter():
        try:
            pid = proc.pid
            active_pids.add(pid)

            if pid not in process_cache:
                process_cache[pid] = proc
                proc.cpu_percent(interval=None)
                cpu_percent = 0.0
            else:
                cpu_percent = proc.cpu_percent(interval=None)

            name = proc.name()
            mem_mb = proc.memory_info().rss / (1024 ** 2)

            processes.append({
                "name": name,
                "cpu_percent": round(cpu_percent, 1),
                "ram_mb": round(mem_mb, 1)
            })

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess
        ):
            continue

    for pid in list(process_cache):
        if pid not in active_pids:
            process_cache.pop(pid, None)

    processes.sort(
        key=lambda item: item["ram_mb"],
        reverse=True
    )

    return jsonify({
        "cpu_percent": cpu,
        "ram_percent": memory.percent,
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "network_rx_mbps": round(rx_mbps, 3),
        "hostname": socket.gethostname(),
        "os": platform.system() + " " + platform.release(),
        "cpu_name": platform.processor() or "Não informado",
        "logical_cores": psutil.cpu_count(logical=True),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
        "processes": processes[:20]
    })


if __name__ == "__main__":
    print("=" * 44)
    print("NEXUS MONITOR - AGENTE WINDOWS")
    print(f"Site local: http://127.0.0.1:{PORT}")
    print(f"API local:  http://127.0.0.1:{PORT}/api/metrics")
    print(f"API na rede: http://IP-LOCAL-DO-PC:{PORT}/api/metrics")
    print("O agente aceita conexões de dispositivos na rede local.")
    print("Não encaminhe a porta 5000 para a internet.")
    print("Pressione Ctrl+C para encerrar.")
    print("=" * 44)

    server = make_server(HOST, PORT, app)
    server.serve_forever()
