#!/bin/bash
# SASEUL Miner Watchdog - 5분마다 cron으로 실행, 죽은 서비스 자동 재시작
# crontab -e 에서 아래 줄 추가:
#   */5 * * * * /opt/saseul-pool-miner/miner_watchdog.sh >> /var/saseul-shared/watchdog.log 2>&1

SERVICES=("saseul-pool-miner" "saseul-cpu-miner" "gpu-autominer")

for svc in "${SERVICES[@]}"; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null && ! systemctl is-active --quiet "$svc"; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $svc is dead, restarting..."
        systemctl restart "$svc"
        sleep 2
        if systemctl is-active --quiet "$svc"; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $svc restarted OK"
        else
            echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $svc restart FAILED"
        fi
    fi
done
