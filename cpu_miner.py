#!/usr/bin/env python3
"""
SASEUL Pool CPU Miner Client

Usage:
  python3 cpu_miner.py --pool pool.takty.kr --port 3333 --address YOUR_SASEUL_ADDRESS
"""
import argparse
import hashlib
import json
import os
import random
import select
import socket
import sys
import time

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


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


class PoolMiner:
    def __init__(self, host, port, address, worker):
        self.host = host
        self.port = port
        self.address = address
        self.worker = worker
        self.sock = None
        self.recv_buf = b''
        self.msg_id = 0
        self.job = None
        self.hashes = 0
        self.shares_sent = 0
        self.shares_ok = 0
        self.start_time = 0

    # ── Network ──

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((self.host, self.port))
        self.recv_buf = b''
        print(f'[*] Connected to {self.host}:{self.port}')

    def send(self, method, params):
        self.msg_id += 1
        line = json.dumps({'id': self.msg_id, 'method': method, 'params': params}) + '\n'
        self.sock.sendall(line.encode())

    def recv_messages(self, timeout=0.05):
        """Non-blocking read of all pending messages."""
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
                raise ConnectionError('Server closed connection')
            self.recv_buf += chunk
            timeout = 0  # drain remaining data immediately

        while b'\n' in self.recv_buf:
            line, self.recv_buf = self.recv_buf.split(b'\n', 1)
            if line.strip():
                msgs.append(json.loads(line))
        return msgs

    def recv_one(self, timeout=10):
        """Blocking read of one message."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self.recv_messages(timeout=0.5)
            if msgs:
                return msgs
        return []

    def recv_reply(self, msg_id, timeout=30):
        """Blocking read until a reply with matching id arrives.
        Other messages (notifications, other replies) are processed along the way."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msgs = self.recv_messages(timeout=0.5)
            for msg in msgs:
                self.process_message(msg)
                if msg.get('id') == msg_id:
                    return msg
        return None

    def process_message(self, msg):
        """Process a single server message (notification or share result)."""
        method = msg.get('method')
        if method == 'mining.notify':
            self.job = msg['params']
            print(f'[+] New job: height={self.job["height"]} '
                  f'diff={self.job["difficulty"]} '
                  f'share_diff={self.job["share_difficulty"]}')
        elif method == 'mining.notify_block':
            print(f'[!!!] BLOCK FOUND at height {msg["params"]["height"]}!')
        elif 'result' in msg:
            # Share result (with or without id)
            if msg['result'] is True:
                self.shares_ok += 1
            elif msg.get('error'):
                print(f'[-] Rejected: {msg["error"]}')

    def process_messages(self, msgs):
        for msg in msgs:
            self.process_message(msg)

    # ── Handshake ──

    def handshake(self):
        # Subscribe
        self.send('mining.subscribe', [])
        sub_id = self.msg_id
        reply = self.recv_reply(sub_id, timeout=30)
        if reply is None:
            print('[!] Subscribe timeout, reconnecting...')
            raise ConnectionError('Subscribe timeout')
        print(f'[*] Subscribed (result: {type(reply.get("result")).__name__})')

        # Authorize
        self.send('mining.authorize', [self.address, self.worker])
        auth_id = self.msg_id
        reply = self.recv_reply(auth_id, timeout=30)
        if reply is None:
            print('[!] Authorize timeout')
            raise ConnectionError('Authorize timeout')

        auth_ok = reply.get('result') is True
        if not auth_ok:
            print(f'[!] Authorization failed: {reply}')
            sys.exit(1)
        print(f'[*] Authorized: {self.address}/{self.worker}')

        # Wait for job (may already be received during handshake)
        if self.job is None:
            print('[*] Waiting for job...')
            for _ in range(20):
                msgs = self.recv_one(timeout=1)
                self.process_messages(msgs)
                if self.job:
                    break

    # ── Mining ──

    def mine_batch(self, batch_size=50000):
        job = self.job
        if not job:
            return

        share_target = job['share_target']
        timestamp = int(time.time() * 1_000_000) + 1_000_000

        header = sha256_obj({
            'height': job['height'],
            'timestamp': timestamp,
            'receipt_root': merkle_root(job.get('receipts', [])),
            'main_height': job['main_height'],
            'main_blockhash': job['main_blockhash'],
            'validator': job['validator'],
            'miner': job['miner'],
        })
        pow_prefix = job['previous_blockhash'] + header

        for _ in range(batch_size):
            nonce_bytes = random.randbytes(12)
            nonce_hex = nonce_bytes.hex()
            nonce = ('00' * 20) + nonce_hex
            root = sha256_str(pow_prefix + nonce)
            self.hashes += 1

            if root <= share_target:
                self.shares_sent += 1
                self.send('mining.submit', {'nonce': nonce, 'timestamp': timestamp})
                print(f'[>] Share #{self.shares_sent} nonce=...{nonce_hex[:16]}')
                return

    def run(self):
        self.connect()
        self.handshake()

        if not self.job:
            print('[!] No job received, exiting')
            return

        self.start_time = time.monotonic()
        self.hashes = 0
        print('[*] Mining started!')

        last_report = time.monotonic()
        last_hashrate_send = time.monotonic()

        while True:
            # Check for new messages (new jobs, share results)
            msgs = self.recv_messages(timeout=0.01)
            if msgs:
                self.process_messages(msgs)

            # Mine a batch
            self.mine_batch()

            now = time.monotonic()

            # Report hashrate to pool every 30s
            if now - last_hashrate_send >= 30:
                elapsed = now - self.start_time
                hr = self.hashes / elapsed if elapsed > 0 else 0
                self.send('mining.hashrate', {'hashrate': hr})
                last_hashrate_send = now

            # Print status every 10s
            if now - last_report >= 10:
                elapsed = now - self.start_time
                hr = self.hashes / elapsed if elapsed > 0 else 0
                if hr >= 1e6:
                    hr_str = f'{hr/1e6:.2f} MH/s'
                elif hr >= 1e3:
                    hr_str = f'{hr/1e3:.1f} KH/s'
                else:
                    hr_str = f'{hr:.0f} H/s'
                print(f'[*] {hr_str} | shares: {self.shares_ok}/{self.shares_sent} '
                      f'| hashes: {self.hashes:,} | height: {self.job["height"] if self.job else "?"}')
                last_report = now


def main():
    parser = argparse.ArgumentParser(description='SASEUL Pool CPU Miner')
    parser.add_argument('--pool', default='pool.takty.kr', help='Pool hostname')
    parser.add_argument('--port', type=int, default=3333, help='Stratum port')
    parser.add_argument('--address', required=True, help='SASEUL miner address')
    parser.add_argument('--worker', default='cpu0', help='Worker name')
    args = parser.parse_args()

    while True:
        try:
            miner = PoolMiner(args.pool, args.port, args.address, args.worker)
            miner.run()
        except KeyboardInterrupt:
            print('\n[*] Stopped by user')
            break
        except Exception as e:
            print(f'[!] Error: {e}, reconnecting in 5s...')
            time.sleep(5)


if __name__ == '__main__':
    main()
