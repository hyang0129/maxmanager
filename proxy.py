#!/usr/bin/env python3
"""Transparent TLS proxy that captures Anthropic API responses.

Redirects api.anthropic.com to localhost via /etc/hosts, terminates TLS,
and relays traffic while dumping all upstream responses to capture files.

Usage:
    sudo python proxy.py [--teardown]
"""

import json
import os
import select
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DOMAINS = ["api.anthropic.com", "claude.ai"]
CA_DIR = Path("/tmp/maxmanager_proxy_ca")
CAPTURE_DIR = Path("/tmp/maxmanager_captures")
REAL_IP_FILE = CA_DIR / "real_ip.txt"
RATE_LIMITS_FILE = Path("/tmp/maxmanager_rate_limits.json")
HOSTS_MARKER = "# maxmanager-proxy"
REAL_IPS: dict[str, str] = {}  # domain -> real IP

_conn_counter = 0
_counter_lock = threading.Lock()

# ---------------------------------------------------------------------------
# DNS / hosts
# ---------------------------------------------------------------------------


def resolve_real_ips() -> dict[str, str]:
    """Resolve real IPs for all domains before overriding /etc/hosts."""
    CA_DIR.mkdir(parents=True, exist_ok=True)
    ips = {}
    for domain in DOMAINS:
        ip_file = CA_DIR / f"real_ip_{domain}.txt"
        if ip_file.exists():
            ip = ip_file.read_text().strip()
            if ip and ip != "127.0.0.1":
                ips[domain] = ip
                continue
        try:
            result = subprocess.run(
                ["dig", "+short", domain, "@8.8.8.8"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith(";") and "." in line:
                    ip_file.write_text(line)
                    ips[domain] = line
                    break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if domain not in ips:
            info = socket.getaddrinfo(domain, 443, socket.AF_INET)
            ip = info[0][4][0]
            if ip == "127.0.0.1":
                raise RuntimeError(f"{domain} resolves to 127.0.0.1. Run --teardown first.")
            ip_file.write_text(ip)
            ips[domain] = ip
    return ips


def add_hosts_entries():
    content = Path("/etc/hosts").read_text()
    if HOSTS_MARKER in content:
        return
    entries = "\n".join(f"127.0.0.1 {d} {HOSTS_MARKER}" for d in DOMAINS)
    Path("/etc/hosts").write_text(content.rstrip("\n") + "\n" + entries + "\n")
    print(f"[proxy] /etc/hosts: redirected {', '.join(DOMAINS)}", flush=True)


def remove_hosts_entries():
    hosts = Path("/etc/hosts")
    lines = hosts.read_text().splitlines(keepends=True)
    hosts.write_text("".join(l for l in lines if HOSTS_MARKER not in l))
    print(f"[proxy] /etc/hosts: removed entries", flush=True)


# ---------------------------------------------------------------------------
# Certs (via openssl CLI)
# ---------------------------------------------------------------------------


def ensure_ca() -> tuple[Path, Path]:
    CA_DIR.mkdir(parents=True, exist_ok=True)
    crt, key = CA_DIR / "ca.crt", CA_DIR / "ca.key"
    if crt.exists() and key.exists():
        return crt, key
    subprocess.run([
        "openssl", "req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048",
        "-keyout", str(key), "-out", str(crt),
        "-days", "365", "-subj", "/CN=MaxManager Proxy CA",
    ], check=True, capture_output=True)
    return crt, key


def ensure_domain_cert() -> tuple[str, str]:
    d = CA_DIR / "domains" / "multi"
    d.mkdir(parents=True, exist_ok=True)
    cert, key = d / "cert.pem", d / "key.pem"
    if cert.exists() and key.exists():
        return str(cert), str(key)

    ca_crt, ca_key = ensure_ca()
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                   check=True, capture_output=True)
    csr = d / "csr.pem"
    subprocess.run(["openssl", "req", "-new", "-key", str(key),
                    "-out", str(csr), "-subj", f"/CN={DOMAINS[0]}"],
                   check=True, capture_output=True)
    san = d / "san.cnf"
    san_dns = ",".join(f"DNS:{dom}" for dom in DOMAINS)
    san.write_text(f"subjectAltName={san_dns}\nbasicConstraints=CA:FALSE\n"
                   f"keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    subprocess.run(["openssl", "x509", "-req", "-in", str(csr),
                    "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
                    "-out", str(cert), "-days", "30", "-extfile", str(san)],
                   check=True, capture_output=True)
    print(f"[proxy] Generated cert for: {', '.join(DOMAINS)}", flush=True)
    return str(cert), str(key)


# ---------------------------------------------------------------------------
# Rate limit scanner (runs on captured data)
# ---------------------------------------------------------------------------


def scan_for_rate_limits(data: bytes) -> None:
    """Scan bytes for rate_limits and write to file if found."""
    text = data.decode("utf-8", errors="replace")
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            line = line[6:]
        if "rate_limits" not in line:
            continue
        try:
            parsed = json.loads(line)
            rl = parsed.get("rate_limits") or parsed.get("message", {}).get("rate_limits")
            if rl and ("five_hour" in rl or "seven_day" in rl):
                five = rl.get("five_hour", {})
                seven = rl.get("seven_day", {})
                out = {
                    "five_hour_used_pct": five.get("used_percentage"),
                    "seven_day_used_pct": seven.get("used_percentage"),
                    "five_hour": five,
                    "seven_day": seven,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                tmp = RATE_LIMITS_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(out, indent=2))
                tmp.rename(RATE_LIMITS_FILE)
                print(f"[proxy] RATE LIMITS CAPTURED: 5hr={five.get('used_percentage','?')}% 7day={seven.get('used_percentage','?')}%", flush=True)
                return
        except (json.JSONDecodeError, AttributeError):
            continue


# ---------------------------------------------------------------------------
# Dumb relay with capture
# ---------------------------------------------------------------------------


def relay_with_capture(client_ssl, upstream_ssl, conn_id: int) -> None:
    """Relay bytes both directions. Capture upstream->client to a file."""
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%H%M%S")
    capture_path = CAPTURE_DIR / f"conn_{conn_id}_{ts}.bin"
    capture_file = open(capture_path, "wb")

    sockets = [client_ssl, upstream_ssl]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], sockets, 120)
            if not readable:
                break
            for s in readable:
                try:
                    data = s.recv(16384)
                except (OSError, ssl.SSLError):
                    return
                if not data:
                    return
                if s is client_ssl:
                    # Client -> upstream (request)
                    upstream_ssl.sendall(data)
                else:
                    # Upstream -> client (response) — capture this
                    client_ssl.sendall(data)
                    capture_file.write(data)
                    capture_file.flush()
                    # Scan for rate limits in the response
                    scan_for_rate_limits(data)
    finally:
        capture_file.close()
        size = capture_path.stat().st_size
        if size == 0:
            capture_path.unlink()
        else:
            print(f"[proxy] Captured {size} bytes -> {capture_path.name}", flush=True)


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------


def handle_connection(client_ssl, conn_id: int) -> None:
    # Determine which domain from SNI
    sni = getattr(client_ssl, 'server_hostname', None) or DOMAINS[0]
    real_ip = REAL_IPS.get(sni, list(REAL_IPS.values())[0])
    print(f"[proxy] [{conn_id}] Connected (sni={sni}, ip={real_ip})", flush=True)
    try:
        upstream_sock = socket.create_connection((real_ip, 443), timeout=15)
        upstream_ctx = ssl.create_default_context()
        upstream_ssl = upstream_ctx.wrap_socket(upstream_sock, server_hostname=sni)
    except Exception as e:
        print(f"[proxy] [{conn_id}] Upstream failed: {e}", flush=True)
        return

    try:
        relay_with_capture(client_ssl, upstream_ssl, conn_id)
    except Exception as e:
        msg = str(e)
        if "EOF" not in msg and "closed" not in msg.lower():
            print(f"[proxy] [{conn_id}] Error: {e}", flush=True)
    finally:
        print(f"[proxy] [{conn_id}] Closed", flush=True)
        try:
            upstream_ssl.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def run_server():
    cert_path, key_path = ensure_domain_cert()
    ca_crt, _ = ensure_ca()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 443))
    srv.listen(32)

    ips_str = ", ".join(f"{d}={ip}" for d, ip in REAL_IPS.items())
    print(f"""
{'='*60}
  MaxManager Capture Proxy
{'='*60}
  Domains:    {', '.join(DOMAINS)}
  Real IPs:   {ips_str}
  CA cert:    {ca_crt}
  Captures:   {CAPTURE_DIR}/
{'='*60}
""", flush=True)

    global _conn_counter

    def accept_loop():
        global _conn_counter
        while True:
            try:
                client_sock, addr = srv.accept()
            except OSError:
                break
            try:
                client_ssl = ctx.wrap_socket(client_sock, server_side=True)
            except ssl.SSLError as e:
                print(f"[proxy] TLS handshake failed: {e}", flush=True)
                client_sock.close()
                continue

            with _counter_lock:
                _conn_counter += 1
                cid = _conn_counter

            threading.Thread(
                target=handle_connection,
                args=(client_ssl, cid),
                daemon=True,
            ).start()

    threading.Thread(target=accept_loop, daemon=True).start()

    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if os.geteuid() != 0:
        print(f"Need root. Run: sudo {sys.executable} {' '.join(sys.argv)}")
        sys.exit(1)

    if "--teardown" in sys.argv:
        remove_hosts_entries()
        return

    global REAL_IPS
    REAL_IPS = resolve_real_ips()
    for d, ip in REAL_IPS.items():
        print(f"[proxy] {d} -> {ip}", flush=True)

    # Delete old single-domain cert to regenerate multi-domain
    old_cert = CA_DIR / "domains" / "api.anthropic.com"
    if old_cert.exists():
        import shutil
        shutil.rmtree(old_cert, ignore_errors=True)

    ensure_ca()
    ensure_domain_cert()
    add_hosts_entries()

    import atexit
    atexit.register(remove_hosts_entries)

    try:
        run_server()
    finally:
        remove_hosts_entries()


if __name__ == "__main__":
    main()
