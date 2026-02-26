#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_miner.py — SASEUL Pool Stratum Client for GPU_AutoMiner
Connects to a SASEUL mining pool via Stratum protocol,
receives jobs, delegates PoW to GPU_AutoMiner via IPC socket,
and submits found shares back to the pool.
"""
from __future__ import annotations
import socket, json, time, os, sys, signal, threading, hashlib, logging
from typing import Any, Dict, Optional

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POOL_HOST = os.environ.get('POOL_HOST', 'pool.takty.kr')
POOL_PORT = int(os.environ.get('POOL_PORT', '3333'))
MINER_ADDRESS = os.environ.get('MINER_ADDRESS', '0570f01f9cdd71575eeed1a998f80cce825290e32270')
WORKER_NAME = os.environ.get('WORKER_NAME', 'gpu-worker')

GPU_SOCK_PATH = os.environ.get('GPU_SOCK_PATH', '/var/saseul-shared/gpu_pow.sock')
GPU_SOCK_TIMEOUT = 120
GPU_STATUS_FILE = os.environ.get('GPU_STATUS_FILE', '/var/saseul-shared/gpu_status.json')
STATUS_FILE = os.environ.get('STATUS_FILE', '/var/saseul-shared/gpu_pool_status.json')
SUCCESS_LOG_DIR = os.environ.get('SUCCESS_LOG_DIR', '/var/saseul-shared/success_logs')

RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60
HASHRATE_LOG_INTERVAL = 30

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('pool_miner')

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Crypto helpers (matching SASEUL's crypto.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HASH_BYTES = 32
HASH_SIZE = HASH_BYTES * 2
HEX_TIME_SIZE = 14
HASH_COUNT = (1 << (HASH_BYTES * 8)) - 1


def sha256(obj: Any) -> str:
    if isinstance(obj, (dict, list)):
        data = json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
    else:
        data = str(obj)
    return hashlib.sha256(data.encode()).hexdigest()


def merkle_root(arr: list) -> str:
    if not arr:
        return sha256('')
    layer = [sha256(x) for x in arr]
    while len(layer) > 1:
        child = []
        for i in range(0, len(layer), 2):
            if i + 1 < len(layer):
                child.append(sha256(layer[i] + layer[i + 1]))
            else:
                child.append(layer[i])
        layer = child
    return layer[0]


def hex_time(utime: int) -> str:
    return f'{utime:0{HEX_TIME_SIZE}x}'


def time_hash(obj: str, timestamp: int) -> str:
    return hex_time(timestamp) + sha256(obj)


def block_header(b: Dict[str, Any]) -> str:
    return sha256({
        'height': b['height'],
        'timestamp': b['timestamp'],
        'receipt_root': merkle_root(b.get('receipts', [])),
        'main_height': b['main_height'],
        'main_blockhash': b['main_blockhash'],
        'validator': b['validator'],
        'miner': b['miner'],
    })


def hash_limit(diff) -> str:
    d = max(int(diff), 1)
    return f'{HASH_COUNT // d:0{HASH_SIZE}x}'


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GPU IPC Client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def gpu_mine_round(pow_left: str, target: str, height: int,
                   main_height: int, job_id: str) -> Optional[Dict]:
    """Send one mine request to GPU_AutoMiner via IPC socket."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(GPU_SOCK_TIMEOUT)
        sock.connect(GPU_SOCK_PATH)
    except (socket.error, OSError) as e:
        log.error(f'GPU IPC connect failed: {e}')
        return None

    try:
        req = {
            'cmd': 'mine',
            'pow_left': pow_left,
            'target': target,
            'height': height,
            'main_height': main_height,
            'job_id': job_id,
        }
        sock.sendall((json.dumps(req) + '\n').encode())

        buf = b''
        while b'\n' not in buf:
            chunk = sock.recv(8192)
            if not chunk:
                return None
            buf += chunk
        line = buf.split(b'\n', 1)[0]
        return json.loads(line.decode())
    except (socket.timeout, socket.error, json.JSONDecodeError, OSError) as e:
        log.error(f'GPU IPC error: {e}')
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Status & Logging
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_total_hashes = 0
_total_shares = 0
_total_blocks = 0
_start_time = time.monotonic()


def read_gpu_hashrate() -> tuple:
    """Read actual GPU hashrate from GPU_AutoMiner status file.
    Returns (total_mhs, gpu_dict) e.g. (1480.4, {'0': 740.2, '1': 740.2})"""
    try:
        with open(GPU_STATUS_FILE) as f:
            data = json.load(f)
        return data.get('total_mhs', 0), data.get('gpus', {})
    except Exception:
        return 0, {}


def write_status(height: int = 0, difficulty: str = '',
                 ghps: float = 0.0, gpu_ids: list = None,
                 pool_connected: bool = False) -> None:
    try:
        mhs = ghps * 1000
        num_gpus = len(gpu_ids) if gpu_ids else 2
        per_gpu_mhs = mhs / num_gpus if num_gpus > 0 else mhs
        data = {
            'ts': int(time.time()),
            'mode': 'pool',
            'pool': f'{POOL_HOST}:{POOL_PORT}',
            'pool_connected': pool_connected,
            'gpus': {str(i): round(per_gpu_mhs, 1) for i in (gpu_ids or [0, 1])},
            'total_mhs': round(mhs, 1),
            'height': height,
            'difficulty': str(difficulty),
            'uptime_sec': int(time.monotonic() - _start_time),
            'total_hashes': _total_hashes,
            'total_shares': _total_shares,
            'total_blocks': _total_blocks,
        }
        tmp = STATUS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        pass


def log_success_jsonl(block: Dict, resp: Dict) -> None:
    try:
        today = time.strftime('%Y-%m-%d')
        os.makedirs(SUCCESS_LOG_DIR, exist_ok=True)
        record = {
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mode': 'pool',
            'result': resp.get('result', ''),
            'job_id': block.get('job_id', ''),
            'height': block.get('height', 0),
            'main_height': block.get('main_height', 0),
            'pow_left': block.get('pow_left', ''),
            'target': block.get('share_target', ''),
            'nonce': resp.get('nonce', ''),
            'hash': resp.get('hash', ''),
            'winner_gpu': resp.get('winner_gpu', -1),
            'gpu_ids': resp.get('gpu_ids', []),
            'nonce_stride': resp.get('nonce_stride', 0),
            'snap_main_h': resp.get('snap_main_h', 0),
            'snap_main_hash': resp.get('snap_main_hash', ''),
            'snap_res_h': resp.get('snap_res_h', 0),
            'snap_res_hash': resp.get('snap_res_hash', ''),
            'elapsed_ms': resp.get('elapsed_ms', 0),
            'total_hashes': resp.get('total_hashes', 0),
            'ghps': resp.get('ghps', 0),
        }
        with open(f'{SUCCESS_LOG_DIR}/{today}.jsonl', 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Stratum Client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class StratumClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.buf = b''
        self.msg_id = 0
        self.current_job: Optional[Dict] = None
        self.job_lock = threading.Lock()
        self.running = True
        self.connected = False

    def connect(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(30)
            self.sock.connect((self.host, self.port))
            self.connected = True
            log.info(f'Connected to pool {self.host}:{self.port}')
            return True
        except (socket.error, OSError) as e:
            log.error(f'Pool connect failed: {e}')
            self.connected = False
            return False

    def disconnect(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _send(self, msg: Dict) -> bool:
        if not self.sock:
            return False
        try:
            self.sock.sendall((json.dumps(msg) + '\n').encode())
            return True
        except (socket.error, OSError) as e:
            log.error(f'Send failed: {e}')
            self.connected = False
            return False

    def _recv_lines(self, timeout: float = 5.0) -> list:
        """Receive all available lines, waiting up to timeout for data."""
        lines = []
        if not self.sock:
            return lines

        # First drain any complete lines already in buffer
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            if line.strip():
                try:
                    lines.append(json.loads(line.decode()))
                except json.JSONDecodeError:
                    pass

        # Then try to read more from socket
        end_time = time.monotonic() + timeout
        while True:
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                break
            self.sock.settimeout(max(remaining, 0.1))
            try:
                chunk = self.sock.recv(8192)
                if not chunk:
                    self.connected = False
                    break
                self.buf += chunk
                while b'\n' in self.buf:
                    line, self.buf = self.buf.split(b'\n', 1)
                    if line.strip():
                        try:
                            lines.append(json.loads(line.decode()))
                        except json.JSONDecodeError:
                            pass
                if lines:
                    # Got at least one line, do a quick non-blocking check for more
                    self.sock.settimeout(0.5)
                    try:
                        chunk = self.sock.recv(8192)
                        if chunk:
                            self.buf += chunk
                            while b'\n' in self.buf:
                                ln, self.buf = self.buf.split(b'\n', 1)
                                if ln.strip():
                                    try:
                                        lines.append(json.loads(ln.decode()))
                                    except json.JSONDecodeError:
                                        pass
                    except socket.timeout:
                        pass
                    break
            except socket.timeout:
                if lines:
                    break
                continue
            except (socket.error, OSError):
                self.connected = False
                break

        # Process any notify messages found
        for line in lines:
            if line.get('method') == 'mining.notify':
                self._handle_notify(line['params'])

        return lines

    def subscribe(self) -> bool:
        self.msg_id += 1
        msg = {'id': self.msg_id, 'method': 'mining.subscribe',
               'params': [f'saseul-pool-miner/1.0']}
        if not self._send(msg):
            return False
        lines = self._recv_lines(10)
        for line in lines:
            if line.get('id') == self.msg_id and line.get('result'):
                log.info('Subscribed to pool')
                return True
        return False

    def authorize(self) -> bool:
        self.msg_id += 1
        auth_id = self.msg_id
        msg = {'id': auth_id, 'method': 'mining.authorize',
               'params': [MINER_ADDRESS, WORKER_NAME]}
        if not self._send(msg):
            return False

        # Receive auth response + possible notify (pool sends both)
        authorized = False
        for attempt in range(3):
            lines = self._recv_lines(5)
            for line in lines:
                if line.get('id') == auth_id:
                    if line.get('result'):
                        authorized = True
                        log.info(f'Authorized: {MINER_ADDRESS} / {WORKER_NAME}')
                    else:
                        log.error(f'Auth rejected: {line.get("error")}')
                        return False
            if authorized and self.current_job:
                break

        return authorized

    def submit_share(self, job_id: int, nonce: str, timestamp: int,
                     blockhash: str) -> bool:
        self.msg_id += 1
        msg = {
            'id': self.msg_id,
            'method': 'mining.submit',
            'params': {
                'miner_address': MINER_ADDRESS,
                'worker_name': WORKER_NAME,
                'job_id': job_id,
                'nonce': nonce,
                'timestamp': timestamp,
                'blockhash': blockhash,
            }
        }
        return self._send(msg)

    def report_hashrate(self, hashrate: float) -> bool:
        self.msg_id += 1
        msg = {
            'id': self.msg_id,
            'method': 'mining.hashrate',
            'params': {'hashrate': hashrate}
        }
        return self._send(msg)

    def _handle_notify(self, params: Dict):
        with self.job_lock:
            self.current_job = params
        log.info(f'New job: height={params.get("height")} '
                 f'diff={params.get("difficulty")} '
                 f'share_diff={params.get("share_difficulty")}')

    def recv_thread(self):
        """Background thread to receive pool notifications.
        Note: mining.notify is handled in _recv_lines automatically."""
        while self.running and self.connected:
            try:
                lines = self._recv_lines(5.0)
                for line in lines:
                    method = line.get('method')
                    if method == 'mining.set_difficulty':
                        log.info(f'Difficulty adjusted: {line.get("params")}')
                    elif line.get('id') and line.get('result') is not None:
                        if line.get('result') is True:
                            log.debug(f'Share accepted (id={line["id"]})')
                        elif line.get('error'):
                            log.warning(f'Share rejected: {line.get("error")}')
            except Exception as e:
                log.error(f'recv_thread error: {e}')
                self.connected = False
                break

    def get_job(self) -> Optional[Dict]:
        with self.job_lock:
            return self.current_job.copy() if self.current_job else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main Mining Loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_shutdown = False


def signal_handler(sig, frame):
    global _shutdown
    log.info('Shutdown signal received')
    _shutdown = True


def mine_with_pool():
    global _total_hashes, _total_shares, _total_blocks, _shutdown

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log.info(f'SASEUL Pool Miner starting')
    log.info(f'Pool: {POOL_HOST}:{POOL_PORT}')
    log.info(f'Address: {MINER_ADDRESS}')
    log.info(f'Worker: {WORKER_NAME}')
    log.info(f'GPU IPC: {GPU_SOCK_PATH}')

    last_log_time = time.monotonic()
    last_ghps = 0.0
    reconnect_delay = RECONNECT_DELAY

    while not _shutdown:
        client = StratumClient(POOL_HOST, POOL_PORT)

        if not client.connect():
            log.info(f'Retrying in {reconnect_delay}s...')
            # Sleep in small increments to respond to shutdown quickly
            for _ in range(int(reconnect_delay)):
                if _shutdown:
                    break
                time.sleep(1)
            reconnect_delay = min(reconnect_delay * 1.5, MAX_RECONNECT_DELAY)
            continue

        reconnect_delay = RECONNECT_DELAY  # Reset on successful connect

        if not client.subscribe():
            log.error('Subscribe failed')
            client.disconnect()
            time.sleep(RECONNECT_DELAY)
            continue

        if not client.authorize():
            log.error('Authorize failed')
            client.disconnect()
            time.sleep(RECONNECT_DELAY)
            continue

        # Wait for initial job
        deadline = time.monotonic() + 10
        while not client.get_job() and time.monotonic() < deadline:
            client._recv_lines(2.0)

        if not client.get_job():
            log.error('No job received from pool')
            client.disconnect()
            time.sleep(RECONNECT_DELAY)
            continue

        # Start receiver thread
        recv_t = threading.Thread(target=client.recv_thread, daemon=True)
        recv_t.start()

        last_job_id = None

        while not _shutdown and client.connected:
            job = client.get_job()
            if job is None:
                time.sleep(0.5)
                continue

            job_id = job.get('job_id')
            if job_id != last_job_id:
                last_job_id = job_id
                log.info(f'Mining job #{job_id}: height={job["height"]}')

            # Build block data for this round
            timestamp = (time.time_ns() // 1_000) + 1_000_000

            block_data = {
                'height': job['height'],
                'timestamp': timestamp,
                'receipts': job.get('receipts', []),
                'main_height': job['main_height'],
                'main_blockhash': job['main_blockhash'],
                'validator': job['validator'],
                'miner': job['miner'],
                'previous_blockhash': job['previous_blockhash'],
            }

            pow_left = job['previous_blockhash'] + block_header(block_data)
            share_target = job.get('share_target', hash_limit(job['difficulty']))

            # Send to GPU
            resp = gpu_mine_round(
                pow_left=pow_left,
                target=share_target,
                height=job['height'],
                main_height=job['main_height'],
                job_id=f'{job["height"]}_{int(time.time())}',
            )

            if resp is None:
                log.warning('GPU_AutoMiner not available, retrying...')
                time.sleep(2)
                continue

            ghps = resp.get('ghps', 0)
            last_ghps = ghps
            gpu_ids = resp.get('gpu_ids', [0, 1])

            th = int(resp.get('total_hashes', 0))
            if th > 0:
                _total_hashes += th

            # Write status (use actual GPU hashrate)
            real_mhs, gpu_info = read_gpu_hashrate()
            write_status(
                height=job.get('height', 0),
                difficulty=job.get('difficulty', ''),
                ghps=real_mhs / 1000,
                gpu_ids=list(gpu_info.keys()) if gpu_info else gpu_ids,
                pool_connected=client.connected,
            )

            # Periodic hashrate log + report to pool
            now = time.monotonic()
            if now - last_log_time >= HASHRATE_LOG_INTERVAL:
                # Read actual GPU hashrate from AutoMiner status
                real_mhs, gpu_info = read_gpu_hashrate()
                real_ghps = real_mhs / 1000
                log.info(f'HashRate: {real_ghps:.2f} GH/s (real) | '
                         f'Height: {job["height"]} | '
                         f'Shares: {_total_shares} | '
                         f'Blocks: {_total_blocks} | '
                         f'Total hashes: {_total_hashes:,}')
                last_log_time = now
                # Report actual hashrate to pool (in H/s)
                client.report_hashrate(real_mhs * 1_000_000)

            if resp.get('result') == 'found':
                nonce = resp.get('nonce', '')
                winner = resp.get('winner_gpu', '?')

                # Compute blockhash
                block_data['nonce'] = nonce
                br = sha256(pow_left + nonce)
                bh = time_hash(br, timestamp)

                log.info(f'SHARE FOUND! GPU{winner} {ghps:.2f} GH/s '
                         f'elapsed: {resp.get("elapsed_ms", 0):.1f}ms')

                # Check if it also meets block difficulty
                block_target = hash_limit(job['difficulty'])
                is_block = br <= block_target

                if is_block:
                    _total_blocks += 1
                    log.info(f'*** BLOCK FOUND! height={job["height"]} ***')

                # Submit share to pool
                client.submit_share(
                    job_id=job_id,
                    nonce=nonce,
                    timestamp=timestamp,
                    blockhash=bh,
                )
                _total_shares += 1

                # Log success
                log_success_jsonl(block_data, resp)

            # Check if job changed (avoid stale work)
            new_job = client.get_job()
            if new_job and new_job.get('job_id') != job_id:
                continue

        log.info('Pool connection lost, reconnecting...')
        client.running = False
        client.disconnect()
        for _ in range(RECONNECT_DELAY):
            if _shutdown:
                break
            time.sleep(1)

    log.info('Pool miner stopped')
    write_status(pool_connected=False)


if __name__ == '__main__':
    mine_with_pool()
