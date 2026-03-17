#!/bin/bash
# Little Wonder V1 - Pi Setup Script
echo "=== Little Wonder V1 Setup ==="

# Install system dependencies
sudo apt-get update
sudo apt-get install -y python3-pip ffmpeg

# Install Python packages
pip3 install --break-system-packages -r requirements.txt

# Create data directory
mkdir -p data

# Copy .env template if not exists
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env — please add your API keys!"
fi

echo ""
echo "=== Setup complete! ==="
echo "1. Edit .env with your API keys"
echo "2. Run: sudo -E python3 app.py"
echo "3. Open browser to: http://localhost:8888/display"
echo ""
