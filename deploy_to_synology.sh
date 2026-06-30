#!/bin/bash
# Deploy CEL system to Synology NAS
# Usage: ./deploy_to_synology.sh admin@192.168.1.133

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 admin@synology-ip"
    echo "Example: $0 admin@192.168.1.133"
    exit 1
fi

SYNOLOGY_USER_HOST="$1"
TARGET_DIR="/volume1/docker/cel-parser"

echo "======================================"
echo "CEL System - Synology Deployment v2.0"
echo "======================================"
echo ""
echo "Target: $SYNOLOGY_USER_HOST"
echo "Directory: $TARGET_DIR"
echo ""
echo "Features:"
echo "  - E66 (individual meters) + E31 (community aggregates)"
echo "  - Batch processing with auto-discovery"
echo "  - Physical-to-virtual meter mapping"
echo ""

# Check if we can connect
echo "Testing SSH connection..."
echo "(You may be prompted for password)"
echo ""

set +e
ssh -o ConnectTimeout=10 "$SYNOLOGY_USER_HOST" "echo 'SSH connection successful'" 2>&1
SSH_RESULT=$?
set -e

if [ $SSH_RESULT -ne 0 ]; then
    echo ""
    echo "======================================"
    echo "ERROR: Cannot connect to $SYNOLOGY_USER_HOST"
    echo "======================================"
    echo ""
    echo "Please ensure:"
    echo "  1. SSH is enabled on Synology:"
    echo "     Control Panel → Terminal & SNMP → Enable SSH service"
    echo "  2. You have admin access to the Synology"
    echo "  3. The IP address is correct: $SYNOLOGY_USER_HOST"
    echo ""
    echo "To avoid password prompts, set up SSH keys:"
    echo "  ssh-copy-id $SYNOLOGY_USER_HOST"
    echo ""
    exit 1
fi

echo ""
echo "✓ Connection verified"
echo ""

# Check if files exist locally
echo "Checking local files..."
REQUIRED_FILES=(
    "scripts/parse_sdat_v16.py"
    "scripts/parse_e31_aggregated.py"
    "scripts/discover_meter_mappings.py"
    "scripts/send_to_victoriametrics.py"
    "scripts/watch_ftproot.py"
    "config/api_config.yaml"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo "ERROR: Missing file: $file"
        echo "Run this script from the cel-community directory"
        exit 1
    fi
done

echo "✓ All required files found"
echo ""

# Read all local files and encode
echo "Reading local files..."
PARSE_SDAT_V16=$(cat scripts/parse_sdat_v16.py | base64 -w 0)
PARSE_E31=$(cat scripts/parse_e31_aggregated.py | base64 -w 0)
DISCOVER_MAPPINGS=$(cat scripts/discover_meter_mappings.py | base64 -w 0)
SEND_VM=$(cat scripts/send_to_victoriametrics.py | base64 -w 0)
WATCH_FTP=$(cat scripts/watch_ftproot.py | base64 -w 0)
API_CONFIG=$(cat config/api_config.yaml | base64 -w 0)
echo "✓ Files loaded (5 scripts + 1 config)"
echo ""

echo "Creating deployment script on Synology..."
# Create the deployment script on remote
ssh "$SYNOLOGY_USER_HOST" "cat > /tmp/cel_deploy.sh" << 'ENDSCRIPT'
#!/bin/bash
set -e

TARGET_DIR="/volume1/docker/cel-parser"

echo "Creating directory structure..."
sudo mkdir -p $TARGET_DIR/scripts
sudo mkdir -p $TARGET_DIR/config
sudo mkdir -p $TARGET_DIR/logs
sudo mkdir -p $TARGET_DIR/data/incoming
sudo mkdir -p $TARGET_DIR/data/archive
sudo mkdir -p $TARGET_DIR/grafana-provisioning/datasources
sudo mkdir -p $TARGET_DIR/grafana-provisioning/dashboards
sudo mkdir -p $TARGET_DIR/grafana-dashboards

echo "Deploying scripts (5 files)..."
echo "$PARSE_SDAT_V16_DATA" | base64 -d | sudo tee $TARGET_DIR/scripts/parse_sdat_v16.py > /dev/null
echo "$PARSE_E31_DATA" | base64 -d | sudo tee $TARGET_DIR/scripts/parse_e31_aggregated.py > /dev/null
echo "$DISCOVER_MAPPINGS_DATA" | base64 -d | sudo tee $TARGET_DIR/scripts/discover_meter_mappings.py > /dev/null
echo "$SEND_VM_DATA" | base64 -d | sudo tee $TARGET_DIR/scripts/send_to_victoriametrics.py > /dev/null
echo "$WATCH_FTP_DATA" | base64 -d | sudo tee $TARGET_DIR/scripts/watch_ftproot.py > /dev/null

echo "Deploying configuration..."
echo "$API_CONFIG_DATA" | base64 -d | sudo tee $TARGET_DIR/config/api_config.yaml > /dev/null

echo "Creating Grafana datasource configuration..."
sudo tee $TARGET_DIR/grafana-provisioning/datasources/victoriametrics.yaml > /dev/null << 'EOF'
apiVersion: 1

datasources:
  - name: VictoriaMetrics
    type: prometheus
    access: proxy
    url: http://victoriametrics:8428
    isDefault: true
    editable: false
EOF

echo "Creating Grafana dashboards configuration..."
sudo tee $TARGET_DIR/grafana-provisioning/dashboards/dashboards.yaml > /dev/null << 'EOF'
apiVersion: 1

providers:
  - name: 'CEL Dashboards'
    orgId: 1
    folder: 'CEL'
    type: file
    disableDeletion: false
    updateIntervalSeconds: 10
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards
EOF

echo "Setting permissions..."
sudo chmod -R 755 $TARGET_DIR

echo "Verifying deployment..."
echo ""
echo "Scripts deployed:"
ls -lh $TARGET_DIR/scripts/*.py | awk '{print "  " $9 " (" $5 ")"}'
echo ""
echo "Config deployed:"
ls -lh $TARGET_DIR/config/*.yaml | awk '{print "  " $9 " (" $5 ")"}'

echo ""
echo "Cleaning up..."
rm -f /tmp/cel_deploy.sh

echo "Deployment complete!"
ENDSCRIPT

echo "✓ Script created"
echo ""

echo "Executing deployment on Synology..."
echo "(You may be prompted for sudo password)"
echo ""

# Execute with environment variables and TTY
ssh -t "$SYNOLOGY_USER_HOST" \
    "export PARSE_SDAT_V16_DATA='$PARSE_SDAT_V16' && \
     export PARSE_E31_DATA='$PARSE_E31' && \
     export DISCOVER_MAPPINGS_DATA='$DISCOVER_MAPPINGS' && \
     export SEND_VM_DATA='$SEND_VM' && \
     export WATCH_FTP_DATA='$WATCH_FTP' && \
     export API_CONFIG_DATA='$API_CONFIG' && \
     chmod +x /tmp/cel_deploy.sh && \
     /tmp/cel_deploy.sh"

echo ""
echo "======================================"
echo "✓ Deployment Successful!"
echo "======================================"
echo ""
echo "Files deployed to: $TARGET_DIR"
echo ""
echo "Next steps:"
echo ""
echo "1. Review configuration (optional):"
echo "   ssh $SYNOLOGY_USER_HOST"
echo "   sudo cat $TARGET_DIR/config/api_config.yaml"
echo ""
echo "2. Deploy Docker stack in Portainer:"
echo "   - Open: http://$(echo $SYNOLOGY_USER_HOST | cut -d@ -f2):9000"
echo "   - Go to: Stacks → Add stack"
echo "   - Name: cel-parser"
echo "   - Copy content from: docker-compose.yml"
echo "   - Click: Deploy the stack"
echo ""
echo "3. Verify containers are running:"
echo "   ssh $SYNOLOGY_USER_HOST 'sudo docker ps | grep cel'"
echo "   Should show: victoriametrics, grafana, cel-parser"
echo ""
echo "4. Check parser logs:"
echo "   ssh $SYNOLOGY_USER_HOST 'sudo docker logs -f cel-parser'"
echo "   Should see:"
echo "     - 'Watcher started. Monitoring for new SDAT files...'"
echo "     - 'Loaded X meter mappings for community members'"
echo ""
echo "5. Access services:"
echo "   - Grafana: http://$(echo $SYNOLOGY_USER_HOST | cut -d@ -f2):3000"
echo "   - VictoriaMetrics: http://$(echo $SYNOLOGY_USER_HOST | cut -d@ -f2):8428"
echo ""
echo "6. Upload XML files to:"
echo "   /volume1/ftproot/"
echo ""
echo "   Files are processed in batches:"
echo "   - 109 files collected (E66 + E31)"
echo "   - Auto-discovery runs before processing"
echo "   - Processed files move to: $TARGET_DIR/data/archive/"
echo ""
echo "Documentation:"
echo "  - PARSING_GUIDE.md - Complete technical reference"
echo "  - README.md - System overview"
echo "  - QUICK_START_SYNOLOGY.md - Deployment guide"
echo ""
