# SASEUL Pool Miner Client

SASEUL 블록체인 풀 마이닝 클라이언트입니다.
**pool.takty.kr** 풀에 연결하여 GPU/CPU 채굴을 수행합니다.

- Pool Dashboard: **http://pool.takty.kr**
- Stratum: `stratum+tcp://pool.takty.kr:3333`
- Fee: 1%

---

## Quick Start

### Ubuntu / Linux (한 줄 설치)

```bash
git clone https://github.com/taktongyoung/SaseulPoolClient.git
cd SaseulPoolClient
bash install.sh --address YOUR_SASEUL_ADDRESS
```

### HiveOS (한 줄 설치)

```bash
git clone https://github.com/taktongyoung/SaseulPoolClient.git
cd SaseulPoolClient
bash install-hiveos.sh --address YOUR_SASEUL_ADDRESS
```

---

## Files

| File | Description |
|------|-------------|
| `pool_miner.py` | GPU 풀 마이너 (systemd 서비스용, GPU_AutoMiner IPC) |
| `gpu_pool_miner.py` | GPU 풀 마이너 (standalone, 명령줄 인자) |
| `cpu_miner.py` | CPU 풀 마이너 (GPU 없이 채굴) |
| `install.sh` | Ubuntu/Debian 자동 설치 스크립트 |
| `install-hiveos.sh` | HiveOS 자동 설치 스크립트 |
| `saseul-pool-miner.service` | GPU 마이너 systemd 서비스 파일 |
| `saseul-cpu-miner.service` | CPU 마이너 systemd 서비스 파일 |
| `miner_watchdog.sh` | 5분마다 서비스 상태 체크 & 자동 재시작 |

---

## Ubuntu / Linux Setup (상세)

### 1. 요구사항

- Ubuntu 20.04+ / Debian 11+
- NVIDIA GPU (Compute Capability 5.0+)
- NVIDIA Driver 525+
- Python 3.8+
- GPU_AutoMiner (별도 설치 필요)

### 2. NVIDIA 드라이버 확인

```bash
nvidia-smi
```

드라이버 미설치 시:
```bash
sudo apt update
sudo apt install nvidia-driver-550
sudo reboot
```

### 3. GPU_AutoMiner 설치

GPU_AutoMiner는 SASEUL PoW 연산을 수행하는 CUDA 바이너리입니다.

```bash
# GPU_AutoMiner 바이너리 배치
mkdir -p /home/$USER/GPU_AutoMiner
# GPU_AutoMiner, cuda_kernel.cu 파일을 이 경로에 복사

# 공유 디렉토리 생성
sudo mkdir -p /var/saseul-shared

# SL.cfg 설정
cat > /var/saseul-shared/SL.cfg << 'EOF'
[wallet]
address=YOUR_SASEUL_ADDRESS

[gpu]
block=256
grid=1024
inner_loop_init=4096

[gpu_tuning]
target_kernel_ms=150.0
min_inner=64
max_inner=4096
smooth_alpha=0.3
EOF

# GPU_AutoMiner 서비스 등록
sudo cat > /etc/systemd/system/gpu-autominer.service << 'EOF'
[Unit]
Description=SASEUL GPU Auto Miner
After=network.target

[Service]
Type=simple
User=$USER
ExecStart=/home/$USER/GPU_AutoMiner/GPU_AutoMiner
WorkingDirectory=/home/$USER/GPU_AutoMiner
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable gpu-autominer
sudo systemctl start gpu-autominer
```

### 4. Pool Miner Client 설치

```bash
git clone https://github.com/taktongyoung/SaseulPoolClient.git
cd SaseulPoolClient
bash install.sh --address YOUR_SASEUL_ADDRESS --worker my-rig
```

### 5. 확인

```bash
# 서비스 상태
systemctl status saseul-pool-miner

# 실시간 로그
journalctl -u saseul-pool-miner -f

# GPU 상태
nvidia-smi
```

---

## HiveOS Setup (상세)

### 1. SSH 접속

HiveOS 대시보드에서 리그의 IP를 확인하고 SSH로 접속합니다.

```bash
ssh user@YOUR_RIG_IP
```

### 2. GPU_AutoMiner 설치

```bash
# GPU_AutoMiner 바이너리를 리그에 업로드
mkdir -p /opt/saseul-miner
# scp 또는 wget으로 GPU_AutoMiner, cuda_kernel.cu 복사

# 공유 디렉토리
mkdir -p /var/saseul-shared

# SL.cfg 설정
cat > /var/saseul-shared/SL.cfg << 'EOF'
[wallet]
address=YOUR_SASEUL_ADDRESS

[gpu]
block=256
grid=1024
inner_loop_init=4096

[gpu_tuning]
target_kernel_ms=150.0
min_inner=64
max_inner=4096
smooth_alpha=0.3
EOF

# 서비스 등록
cat > /etc/systemd/system/gpu-autominer.service << 'EOF'
[Unit]
Description=SASEUL GPU Auto Miner
After=network.target

[Service]
Type=simple
ExecStart=/opt/saseul-miner/GPU_AutoMiner
WorkingDirectory=/opt/saseul-miner
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable gpu-autominer
systemctl start gpu-autominer
```

### 3. Pool Miner 설치

```bash
git clone https://github.com/taktongyoung/SaseulPoolClient.git
cd SaseulPoolClient
bash install-hiveos.sh --address YOUR_SASEUL_ADDRESS
```

### 4. HiveOS 재부팅 시 자동 시작

설치 스크립트가 systemd 서비스를 등록하므로 재부팅 후에도 자동으로 시작됩니다.

설정 파일은 `/hive-config/saseul-pool-miner.conf`에 백업됩니다.

---

## CPU Miner

GPU 없이 CPU로 풀 채굴을 합니다. GPU와 동시에 실행하여 추가 해시파워를 얻을 수 있습니다.

### 직접 실행

```bash
python3 cpu_miner.py --address YOUR_ADDRESS --worker cpu0
```

### systemd 서비스로 등록

```bash
# 서비스 파일 복사 후 주소 수정
sudo cp saseul-cpu-miner.service /etc/systemd/system/
sudo nano /etc/systemd/system/saseul-cpu-miner.service
# --address YOUR_SASEUL_ADDRESS 부분을 본인 주소로 변경

sudo systemctl daemon-reload
sudo systemctl enable saseul-cpu-miner
sudo systemctl start saseul-cpu-miner

# 로그 확인
journalctl -u saseul-cpu-miner -f
```

> CPU 마이닝은 GPU 대비 매우 느립니다 (약 1/1000). GPU와 병행 사용을 권장합니다.

---

## GPU Standalone Miner (서비스 없이 직접 실행)

```bash
python3 gpu_pool_miner.py \
  --pool pool.takty.kr \
  --port 3333 \
  --address YOUR_ADDRESS \
  --worker my-gpu \
  --gpu-sock /var/saseul-shared/gpu_pow.sock
```

---

## Monitoring

```bash
# 서비스 로그
journalctl -u saseul-pool-miner -f

# GPU 상태
nvidia-smi

# Pool 상태
curl -s http://pool.takty.kr/api/status | python3 -m json.tool

# 내 마이너 상태
curl -s http://pool.takty.kr/api/miners/YOUR_ADDRESS | python3 -m json.tool
```

---

## Watchdog (자동 재시작)

마이너 서비스가 죽으면 5분마다 자동으로 재시작합니다.

```bash
# watchdog 스크립트 복사
sudo cp miner_watchdog.sh /opt/saseul-pool-miner/
sudo chmod +x /opt/saseul-pool-miner/miner_watchdog.sh

# cron 등록 (5분마다 실행)
(crontab -l 2>/dev/null; echo "*/5 * * * * /opt/saseul-pool-miner/miner_watchdog.sh >> /var/saseul-shared/watchdog.log 2>&1") | crontab -

# watchdog 로그 확인
tail -f /var/saseul-shared/watchdog.log
```

---

## Troubleshooting

### GPU_AutoMiner IPC 소켓 연결 실패
```
[GPU] IPC connect failed: [Errno 2] No such file or directory
```
- GPU_AutoMiner가 실행 중인지 확인: `systemctl status gpu-autominer`
- 소켓 파일 확인: `ls -la /var/saseul-shared/gpu_pow.sock`

### Share rejected
```
[Stratum] Rejected: low_difficulty
```
- 정상적인 상황입니다. GPU가 share target 이하의 해시를 찾지 못한 경우입니다.

### 풀 연결 실패
```
Pool connect failed: [Errno 111] Connection refused
```
- 풀 서버 상태 확인: `curl http://pool.takty.kr/api/status`
- 방화벽 확인: `telnet pool.takty.kr 3333`

---

## GPU Tuning (SL.cfg)

| GPU | block | grid | inner_loop_init |
|-----|-------|------|-----------------|
| GTX 1660 SUPER | 256 | 1024 | 4096 |
| RTX 3060 | 256 | 2048 | 8192 |
| RTX 3090 | 384 | 4096 | 16384 |
| RTX 4090 | 512 | 8192 | 16384 |
| RTX 5090 | 512 | 8192 | 16384 |

> 실제 성능은 하드웨어에 따라 다를 수 있습니다. `target_kernel_ms=150.0`을 기준으로 AutoMiner가 자동 조정합니다.

---

## License

MIT License
