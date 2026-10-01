#!/bin/bash
set -e

echo "=================================================================="
echo "    Polymarket <-> Kalshi Arbitrage Bot EC2 Setup Script"
echo "=================================================================="

# 1. Update OS packages
echo "[1/5] Updating system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git curl ufw

# 2. Setup Virtual Environment
echo "[2/5] Creating Python virtual environment..."
cd /home/ubuntu/Kalshi_POLY_ARB
python3 -m venv venv
source venv/bin/activate

# 3. Install requirements
echo "[3/5] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Install systemd service
echo "[4/5] Configuring systemd service..."
sudo cp deploy/kalshi-arb.service /etc/systemd/system/kalshi-arb.service
sudo systemctl daemon-reload
sudo systemctl enable kalshi-arb.service

echo "[5/5] Setup complete!"
echo "=================================================================="
echo "NEXT STEPS:"
echo "1. Upload your .env and kalshi_key.pem from your local PC using scp:"
echo "   scp -i your-key.pem .env ubuntu@<EC2-IP>:/home/ubuntu/Kalshi_POLY_ARB/.env"
echo "   scp -i your-key.pem config/kalshi_key.pem ubuntu@<EC2-IP>:/home/ubuntu/Kalshi_POLY_ARB/config/kalshi_key.pem"
echo ""
echo "2. Start the bot service:"
echo "   sudo systemctl start kalshi-arb.service"
echo ""
echo "3. Check service status & logs:"
echo "   sudo systemctl status kalshi-arb.service"
echo "   journalctl -u kalshi-arb.service -f"
echo "=================================================================="
