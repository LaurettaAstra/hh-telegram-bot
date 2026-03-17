#!/bin/bash
# Deploy script - run on server after SSH
# Usage: ssh user@server "cd /path/to/hh_vacancy_bot && bash deploy.sh"

set -e
echo "=== Deploying HH Vacancy Bot ==="

# Pull latest changes
git pull

# Restart bot (adjust service name if different)
if systemctl is-active --quiet hh-bot 2>/dev/null; then
    echo "Restarting systemd service hh-bot..."
    sudo systemctl restart hh-bot
    echo "Service restarted. Checking status..."
    sudo systemctl status hh-bot --no-pager
elif [ -f "restart.sh" ]; then
    echo "Running restart.sh..."
    bash restart.sh
else
    echo "No systemd service 'hh-bot' found. Restart the Python process manually:"
    echo "  pkill -f 'python main.py' || true"
    echo "  nohup python main.py &"
fi

echo "=== Deploy complete ==="
