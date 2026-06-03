#!/bin/bash
# install_sat_worker.sh — ejecutar en cada nodo (VPS1, VPS2, VPSx, Spark)
# Uso: bash install_sat_worker.sh <node_id> <node_ip> [sat_health_token]
#
# Nodos:
#   VPS1:  bash install_sat_worker.sh vps1  10.20.0.1
#   VPS2:  bash install_sat_worker.sh vps2  10.20.0.2
#   VPSx:  bash install_sat_worker.sh vpsx  10.20.0.100
#   Spark: bash install_sat_worker.sh spark 10.20.0.6

set -e

NODE_ID=${1:-"unknown"}
NODE_IP=${2:-"0.0.0.0"}
SAT_HEALTH_TOKEN=${3:-""}

echo "=== Instalando SAT Health Worker en $NODE_ID ($NODE_IP) ==="

# Crear directorio
mkdir -p /opt/sat-health-worker

# Instalar dependencias Python
pip install fastapi uvicorn playwright pydantic

# Instalar browsers de Playwright
playwright install chromium
playwright install-deps chromium

# Copiar worker.py (asume que está en el directorio actual o se transfiere via SCP)
if [ -f "worker.py" ]; then
    cp worker.py /opt/sat-health-worker/worker.py
fi

# Crear servicio systemd
cat > /etc/systemd/system/sat-health-worker.service << EOF
[Unit]
Description=SAT Health Worker - ${NODE_ID}
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sat-health-worker
Environment=SAT_WORKER_NODE_ID=${NODE_ID}
Environment=SAT_WORKER_NODE_IP=${NODE_IP}
Environment=SAT_HEALTH_TOKEN=${SAT_HEALTH_TOKEN}
ExecStart=/usr/local/bin/uvicorn worker:app --host 0.0.0.0 --port 8300 --workers 1
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Habilitar e iniciar
systemctl daemon-reload
systemctl enable sat-health-worker
systemctl start sat-health-worker

echo "=== Worker $NODE_ID instalado y corriendo en puerto 8300 ==="
systemctl status sat-health-worker --no-pager
