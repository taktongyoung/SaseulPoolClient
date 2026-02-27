#!/usr/bin/env python3
"""
SASEUL GPU Proxy Server

Exposes the local GPU_AutoMiner (Unix socket) over TCP so that
remote pool miners can use the GPU for PoW computation.

Architecture:
  [Remote miner] --TCP:{port}--> [gpu_proxy.py] --Unix socket--> [GPU_AutoMiner]

Protocol: Same JSON-line protocol as GPU_AutoMiner IPC.
  Request:  {"cmd":"mine","pow_left":"...","target":"...","height":N,...}\n
  Response: {"result":"found"/"continue","nonce":"...","ghps":N,...}\n

Security:
  --allow-ips  Comma-separated whitelist of allowed IPs (default: all)

Usage:
  python3 gpu_proxy.py --port 9800
  python3 gpu_proxy.py --port 9800 --allow-ips 192.168.1.10,10.0.0.5
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ── Constants ──
DEFAULT_PORT = 9800
DEFAULT_GPU_SOCK = '/var/saseul-shared/gpu_pow.sock'
GPU_TIMEOUT = 120
CLIENT_TIMEOUT = 30


def forward_to_gpu(gpu_sock_path, request_line):
    """Forward a single request to GPU_AutoMiner and return the response."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(GPU_TIMEOUT)
    try:
        sock.connect(gpu_sock_path)
        sock.sendall(request_line)
        buf = b''
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                return None
            buf += chunk
            if b'\n' in buf:
                line, _ = buf.split(b'\n', 1)
                return line + b'\n'
    except (socket.timeout, socket.error) as e:
        print(f'[GPU] IPC error: {e}')
        return None
    finally:
        sock.close()


def handle_client(client_sock, client_addr, gpu_sock_path, stats):
    """Handle a single TCP client connection (1 request-response)."""
    addr_str = f'{client_addr[0]}:{client_addr[1]}'
    try:
        client_sock.settimeout(CLIENT_TIMEOUT)
        buf = b''
        while b'\n' not in buf:
            chunk = client_sock.recv(8192)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 65536:
                print(f'[{addr_str}] Request too large, dropping')
                return

        request_line, _ = buf.split(b'\n', 1)
        request_line = request_line + b'\n'

        # Parse to validate JSON
        try:
            req = json.loads(request_line)
        except json.JSONDecodeError:
            err = json.dumps({'error': 'invalid JSON'}) + '\n'
            client_sock.sendall(err.encode())
            return

        cmd = req.get('cmd', '?')
        height = req.get('height', '?')
        print(f'[{addr_str}] cmd={cmd} height={height}')

        stats['requests'] += 1

        # Forward to GPU_AutoMiner
        resp_line = forward_to_gpu(gpu_sock_path, request_line)
        if resp_line is None:
            err = json.dumps({'error': 'GPU unavailable'}) + '\n'
            client_sock.sendall(err.encode())
            stats['gpu_errors'] += 1
            return

        client_sock.sendall(resp_line)

        # Log result
        try:
            resp = json.loads(resp_line)
            result = resp.get('result', '?')
            ghps = resp.get('ghps', 0)
            if result == 'found':
                print(f'[{addr_str}] FOUND nonce={resp.get("nonce", "")[:16]}... {ghps:.2f} GH/s')
                stats['found'] += 1
            else:
                stats['not_found'] += 1
        except json.JSONDecodeError:
            pass

    except (socket.timeout, socket.error, ConnectionError) as e:
        print(f'[{addr_str}] Connection error: {e}')
    finally:
        client_sock.close()


def status_printer(stats, interval=60):
    """Periodically print stats."""
    while True:
        time.sleep(interval)
        uptime = time.monotonic() - stats['start_time']
        hours = uptime / 3600
        print(f'[Stats] uptime={hours:.1f}h requests={stats["requests"]} '
              f'found={stats["found"]} not_found={stats["not_found"]} '
              f'gpu_errors={stats["gpu_errors"]} '
              f'clients={stats["active_clients"]}')


def main():
    parser = argparse.ArgumentParser(description='SASEUL GPU Proxy Server')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'TCP listen port (default: {DEFAULT_PORT})')
    parser.add_argument('--bind', default='0.0.0.0',
                        help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--gpu-sock', default=DEFAULT_GPU_SOCK,
                        help=f'GPU_AutoMiner IPC socket path')
    parser.add_argument('--allow-ips', default='',
                        help='Comma-separated whitelist of allowed IPs (empty = allow all)')
    parser.add_argument('--max-clients', type=int, default=10,
                        help='Max concurrent clients (default: 10)')
    args = parser.parse_args()

    allowed_ips = set()
    if args.allow_ips:
        allowed_ips = {ip.strip() for ip in args.allow_ips.split(',')}
        print(f'[*] Allowed IPs: {allowed_ips}')
    else:
        print(f'[*] Allowing all IPs (use --allow-ips to restrict)')

    # Check GPU socket
    if not os.path.exists(args.gpu_sock):
        print(f'[!] Warning: GPU socket not found: {args.gpu_sock}')
        print(f'[!] Make sure GPU_AutoMiner is running')

    stats = {
        'requests': 0,
        'found': 0,
        'not_found': 0,
        'gpu_errors': 0,
        'active_clients': 0,
        'start_time': time.monotonic(),
    }

    # Start stats printer
    t = threading.Thread(target=status_printer, args=(stats,), daemon=True)
    t.start()

    # Start TCP server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.bind, args.port))
    server.listen(args.max_clients)

    print(f'[*] GPU Proxy listening on {args.bind}:{args.port}')
    print(f'[*] GPU socket: {args.gpu_sock}')
    print(f'[*] Max concurrent clients: {args.max_clients}')

    semaphore = threading.Semaphore(args.max_clients)

    while True:
        try:
            client_sock, client_addr = server.accept()
        except KeyboardInterrupt:
            print('\n[*] Shutting down')
            break

        client_ip = client_addr[0]

        # IP whitelist check
        if allowed_ips and client_ip not in allowed_ips:
            print(f'[{client_ip}] Blocked (not in allow list)')
            client_sock.close()
            continue

        # Concurrency limit
        if not semaphore.acquire(blocking=False):
            print(f'[{client_ip}] Rejected (max clients reached)')
            try:
                err = json.dumps({'error': 'server busy'}) + '\n'
                client_sock.sendall(err.encode())
            except Exception:
                pass
            client_sock.close()
            continue

        stats['active_clients'] += 1

        def client_thread(cs, ca):
            try:
                handle_client(cs, ca, args.gpu_sock, stats)
            finally:
                stats['active_clients'] -= 1
                semaphore.release()

        t = threading.Thread(target=client_thread, args=(client_sock, client_addr))
        t.daemon = True
        t.start()

    server.close()


if __name__ == '__main__':
    main()
