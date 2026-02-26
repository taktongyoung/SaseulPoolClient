#!/usr/bin/env python3
"""
SASEUL Pool GPU Miner Client
Uses GPU_AutoMiner (C+CUDA) via IPC socket for high-performance mining,
and submits shares to the pool's Stratum server.

Usage:
  python3 gpu_pool_miner.py --pool pool.takty.kr --port 3333 --address YOUR_SASEUL_ADDRESS
"""
import argparse
import hashlib
import json
import os
import select
import socket
import sys
import time

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ── Constants ──
SOCK_PATH = '/var/saseul-shared/gpu_pow.sock'
SOCK_TIMEOUT = 120

# ── SHA256 Helpers (matching SASEUL spec) ──

def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def to_json(obj) -> str:
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)

def sha256_obj(obj) -> str:
    if isinstance(obj, (dict, list)):
        return sha256_str(to_json(obj))
    return sha256_str(str(obj))

def merkle_root(arr):
    if not arr:
        return sha256_obj('')
    layer = [sha256_obj(x) for x in arr]
    while len(layer) > 1:
        child = []
        for i in range(0, len(layer), 2):
            if i + 1 < len(layer):
                child.append(sha256_str(layer[i] + layer[i + 1]))
            else:
                child.append(layer[i])
        layer = child
    return layer[0]

def block_header_hash(height, timestamp, receipts, main_height, main_blockhash, validator, miner):
    return sha256_obj({
        'height': height,
        'timestamp': timestamp,
        'receipt_root': merkle_root(receipts),
        'main_height': main_height,
        'main_blockhash': main_blockhash,
        'validator': validator,
        'miner': miner,
    })

def hex_time(utime):
    return format(utime, '014x')

def time_hash(obj_str, timestamp):
    return hex_time(timestamp) + sha256_str(obj_str)

HASH_COUNT = (1 << 256) - 1

def hash_limit(difficulty):
    d = int(difficulty)
    if d == 0:
        return '0' * 64
    return format(HASH_COUNT // d, '064x')


# ── Stratum Client ──

class StratumClient:
    def __init__(self, host, port, address, worker):
        self.host = host
        self.port = port
        self.address = address
        self.worker = worker
        self.sock = None
        self.recv_buf = b''
        self.msg_id = 0
        self.job = None
        self.shares_sent = 0
        self.shares_accepted = 0

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((self.host, self.port))
        self.recv_buf = b''
        print(f'[Stratum] Connected to {self.host}:{self.port}')

    def send(self, method, params):
        self.msg_id += 1
        line = json.dumps({'id': self.msg_id, 'method': method, 'params': params}) + '\n'
        self.sock.sendall(line.encode())

    def recv_messages(self, timeout=0.05):
        msgs = []
        while True:
            ready, _, _ = select.select([self.sock], [], [], timeout)
            if not ready:
                break
            try:
                chunk = self.sock.recv(16384)
            except (socket.timeout, BlockingIOError):
                break
            if not chunk:
                raise ConnectionError('Server closed')
            self.recv_buf += chunk
            timeout = 0
        while b'\n' in self.recv_buf:
            line, self.recv_buf = self.recv_buf.split(b'\n', 1)
            if line.strip():
                msgs.append(json.loads(line))
        return msgs

    def recv_one(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self.recv_messages(timeout=0.5)
            if msgs:
                return msgs
        return []

    def process_messages(self, msgs):
        for msg in msgs:
            method = msg.get('method')
            if method == 'mining.notify':
                self.job = msg['params']
                print(f'[Stratum] New job: height={self.job["height"]} '
                      f'diff={self.job["difficulty"]} share_diff={self.job["share_difficulty"]}')
            elif method == 'mining.notify_block':
                print(f'[Stratum] !!! BLOCK FOUND at height {msg["params"]["height"]}!')
            elif 'result' in msg:
                if msg['result'] is True:
                    self.shares_accepted += 1
                elif msg.get('error'):
                    print(f'[Stratum] Rejected: {msg["error"]}')

    def handshake(self):
        self.send('mining.subscribe', [])
        msgs = self.recv_one()
        self.process_messages(msgs)

        self.send('mining.authorize', [self.address, self.worker])
        msgs = self.recv_one()
        self.process_messages(msgs)

        auth_ok = any(m.get('result') is True for m in msgs)
        if not auth_ok:
            print('[Stratum] Auth failed!')
            sys.exit(1)
        print(f'[Stratum] Authorized: {self.address}/{self.worker}')

        if self.job is None:
            for _ in range(20):
                msgs = self.recv_one(timeout=1)
                self.process_messages(msgs)
                if self.job:
                    break

    def submit_share(self, nonce, timestamp):
        self.shares_sent += 1
        self.send('mining.submit', {'nonce': nonce, 'timestamp': timestamp})

    def report_hashrate(self, hashrate):
        self.send('mining.hashrate', {'hashrate': hashrate})


# ── GPU IPC Client ──

class GPUMiner:
    def __init__(self, sock_path=SOCK_PATH):
        self.sock_path = sock_path

    def mine_round(self, pow_left, target, height, main_height):
        """Send one mine request to GPU_AutoMiner, get result."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCK_TIMEOUT)
        try:
            sock.connect(self.sock_path)
        except (socket.error, OSError) as e:
            print(f'[GPU] IPC connect failed: {e}')
            return None

        req = {
            'cmd': 'mine',
            'pow_left': pow_left,
            'target': target,
            'height': height,
            'main_height': main_height,
            'job_id': f'{height}_{int(time.time())}',
        }

        try:
            sock.sendall((json.dumps(req) + '\n').encode())
            buf = b''
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    return None
                buf += chunk
                if b'\n' in buf:
                    line, _ = buf.split(b'\n', 1)
                    return json.loads(line)
        except (socket.timeout, socket.error, json.JSONDecodeError) as e:
            print(f'[GPU] IPC error: {e}')
            return None
        finally:
            sock.close()


# ── Main Loop ──

def main():
    parser = argparse.ArgumentParser(description='SASEUL Pool GPU Miner')
    parser.add_argument('--pool', default='pool.takty.kr', help='Pool hostname')
    parser.add_argument('--port', type=int, default=3333, help='Stratum port')
    parser.add_argument('--address', required=True, help='SASEUL miner address')
    parser.add_argument('--worker', default='gpu0', help='Worker name')
    parser.add_argument('--gpu-sock', default=SOCK_PATH, help='GPU_AutoMiner IPC socket path')
    args = parser.parse_args()

    stratum = StratumClient(args.pool, args.port, args.address, args.worker)
    gpu = GPUMiner(args.gpu_sock)

    total_hashes = 0
    start_time = time.monotonic()
    last_report = time.monotonic()

    while True:
        try:
            stratum.connect()
            stratum.handshake()

            if not stratum.job:
                print('[!] No job received')
                time.sleep(5)
                continue

            print('[*] GPU mining started!')

            while True:
                # Check for new messages from pool
                msgs = stratum.recv_messages(timeout=0.01)
                if msgs:
                    stratum.process_messages(msgs)

                job = stratum.job
                if not job:
                    time.sleep(1)
                    continue

                timestamp = int(time.time() * 1_000_000) + 1_000_000

                # Compute pow_left and target using SHARE difficulty
                header = block_header_hash(
                    job['height'], timestamp, job.get('receipts', []),
                    job['main_height'], job['main_blockhash'],
                    job['validator'], job['miner'],
                )
                pow_left = job['previous_blockhash'] + header

                # Use SHARE target for GPU to find shares faster
                share_target = job['share_target']
                block_target = job['block_target']

                # Send to GPU_AutoMiner with share difficulty target
                resp = gpu.mine_round(pow_left, share_target, job['height'], job['main_height'])

                if resp is None:
                    print('[GPU] Not available, retrying in 2s...')
                    time.sleep(2)
                    continue

                ghps = resp.get('ghps', 0)
                th = int(resp.get('total_hashes', 0))
                total_hashes += th

                if resp.get('result') == 'found':
                    nonce = resp.get('nonce', '')
                    winner = resp.get('winner_gpu', '?')

                    # Verify: does it meet block difficulty too?
                    padded_nonce = nonce.ljust(64, '0') if len(nonce) < 64 else nonce
                    root = sha256_str(pow_left + padded_nonce)
                    meets_block = root <= block_target

                    if meets_block:
                        print(f'[!!!] BLOCK FOUND! height={job["height"]} GPU{winner} {ghps:.2f} GH/s')
                    else:
                        print(f'[>] Share found by GPU{winner} | {ghps:.2f} GH/s')

                    stratum.submit_share(nonce, timestamp)

                # Status report
                now = time.monotonic()
                if now - last_report >= 30:
                    # Try to read actual GPU hashrate from AutoMiner status
                    real_hr = ghps * 1e9
                    try:
                        gpu_status_file = os.path.join(
                            os.path.dirname(args.gpu_sock), 'gpu_status.json')
                        with open(gpu_status_file) as f:
                            gpu_st = json.load(f)
                        real_hr = gpu_st.get('total_mhs', 0) * 1e6  # MH/s -> H/s
                        real_ghps = real_hr / 1e9
                        print(f'[*] GPU {real_ghps:.2f} GH/s | '
                              f'shares: {stratum.shares_accepted}/{stratum.shares_sent} | '
                              f'total hashes: {total_hashes:,} | '
                              f'height: {job["height"]}')
                    except Exception:
                        print(f'[*] GPU {ghps:.2f} GH/s | '
                              f'shares: {stratum.shares_accepted}/{stratum.shares_sent} | '
                              f'total hashes: {total_hashes:,} | '
                              f'height: {job["height"]}')
                    stratum.report_hashrate(real_hr)
                    last_report = now

        except KeyboardInterrupt:
            print('\n[*] Stopped by user')
            break
        except Exception as e:
            print(f'[!] Error: {e}, reconnecting in 5s...')
            time.sleep(5)


if __name__ == '__main__':
    main()
