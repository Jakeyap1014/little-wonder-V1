#!/bin/bash
# Little Wonder V1 - Pi Setup Script
echo "=== Little Wonder V1 Setup ==="
sudo apt-get update
sudo apt-get install -y python3-pip ffmpeg
pip3 install --break-system-packages -r requirements.txt
mkdir -p data
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env - please add your API keys!"
fi
echo "Setup complete! Edit .env then run: sudo -E python3 app.py"
