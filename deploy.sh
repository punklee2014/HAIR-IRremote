#!/usr/bin/env bash
# Quick deploy: copy hair integration to HA and restart.
# Usage: ./deploy.sh 192.168.1.100
set -euo pipefail
HA_HOST="${1:?Usage: ./deploy.sh <ha_ip>}"
echo "Deploying to ${HA_HOST}..."
scp -r custom_components/hair "root@${HA_HOST}:/config/custom_components/"
echo "Restarting HA..."
ssh "root@${HA_HOST}" "ha core restart"
echo "Done."
