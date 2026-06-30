#!/bin/bash
#
# Reprocess All CEL Data
#
# This script:
# 1. Stops the parser
# 2. Deletes all CEL data from VictoriaMetrics
# 3. Moves files from archive to incoming
# 4. Starts the parser (files will be reprocessed and moved back to archive)
# 5. Shows progress

set -e

SYNOLOGY_HOST="${SYNOLOGY_HOST:-synology}"
VM_URL="${VM_URL:-http://localhost:8428}"

echo "=============================================="
echo "CEL Data Reprocessing"
echo "=============================================="
echo
echo "This will:"
echo "  - Delete ALL cel_energy_* metrics from VictoriaMetrics"
echo "  - Delete ALL energy_community_aggregate_kwh metrics (E31)"
echo "  - Move files from archive to incoming"
echo "  - Reprocess all files with correct meter mappings"
echo "  - Files will be moved back to archive after processing"
echo
read -p "Are you sure you want to continue? (yes/no): " confirm

if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 1
fi

echo
echo "Step 1: Stopping parser..."
echo "----------------------------------------------"
ssh $SYNOLOGY_HOST "docker stop cel-parser"
echo "✓ Parser stopped"

echo
echo "Step 2: Deleting CEL data from VictoriaMetrics..."
echo "----------------------------------------------"

# Delete E66 individual meter data
echo "Deleting E66 metrics (individual meters)..."
ssh $SYNOLOGY_HOST "curl -X POST '$VM_URL/api/v1/admin/tsdb/delete_series?match[]=cel_energy_consumed_kwh'"
ssh $SYNOLOGY_HOST "curl -X POST '$VM_URL/api/v1/admin/tsdb/delete_series?match[]=cel_energy_produced_kwh'"
ssh $SYNOLOGY_HOST "curl -X POST '$VM_URL/api/v1/admin/tsdb/delete_series?match[]=cel_energy_grid_import_kwh'"
ssh $SYNOLOGY_HOST "curl -X POST '$VM_URL/api/v1/admin/tsdb/delete_series?match[]=cel_energy_grid_export_kwh'"
ssh $SYNOLOGY_HOST "curl -X POST '$VM_URL/api/v1/admin/tsdb/delete_series?match[]=cel_energy_local_import_kwh'"
ssh $SYNOLOGY_HOST "curl -X POST '$VM_URL/api/v1/admin/tsdb/delete_series?match[]=cel_energy_local_export_kwh'"

# Delete E31 community aggregate data
echo "Deleting E31 metrics (community aggregates)..."
ssh $SYNOLOGY_HOST "curl -X POST '$VM_URL/api/v1/admin/tsdb/delete_series?match[]=energy_community_aggregate_kwh'"

echo "✓ Data deletion triggered (will complete asynchronously)"

echo
echo "Step 3: Moving files from archive to incoming..."
echo "----------------------------------------------"
file_count=$(ssh $SYNOLOGY_HOST "ls -1 /volume1/docker/cel-parser/data/archive/*.xml 2>/dev/null | wc -l")
echo "Found $file_count files in archive"

ssh $SYNOLOGY_HOST "mv /volume1/docker/cel-parser/data/archive/*.xml /volume1/docker/cel-parser/data/incoming/ 2>/dev/null || true"

incoming_count=$(ssh $SYNOLOGY_HOST "ls -1 /volume1/docker/cel-parser/data/incoming/*.xml 2>/dev/null | wc -l")
echo "✓ Moved $incoming_count files to incoming/"
echo "✓ Archive cleared (files will be moved back after processing)"

echo
echo "Step 4: Starting parser with correct mappings..."
echo "----------------------------------------------"
ssh $SYNOLOGY_HOST "docker start cel-parser"
echo "✓ Parser started"

echo
echo "Step 5: Monitoring processing (press Ctrl+C to stop watching)..."
echo "----------------------------------------------"
echo
ssh $SYNOLOGY_HOST "docker logs -f cel-parser" &
LOG_PID=$!

# Wait a bit, then show progress
sleep 5
echo
echo "----------------------------------------------"
echo "Checking progress..."
echo "----------------------------------------------"

while true; do
    sleep 10
    remaining=$(ssh $SYNOLOGY_HOST "ls -1 /volume1/docker/cel-parser/data/incoming/*.xml 2>/dev/null | wc -l")
    processed=$(ssh $SYNOLOGY_HOST "ls -1 /volume1/docker/cel-parser/data/archive/*.xml 2>/dev/null | wc -l")

    if [ "$remaining" -eq 0 ]; then
        echo
        echo "✓ All files processed!"
        echo "  Total processed: $processed files"
        kill $LOG_PID 2>/dev/null || true
        break
    fi

    echo "$(date '+%H:%M:%S') - Remaining: $remaining | Processed: $processed"
done

echo
echo "=============================================="
echo "Reprocessing Complete!"
echo "=============================================="
echo
echo "Next steps:"
echo "1. Verify data in VictoriaMetrics:"
echo "   curl '$VM_URL/api/v1/series?match[]=cel_energy_consumed_kwh' | jq"
echo
echo "2. Check Grafana dashboards"
echo
echo "All files have been reprocessed and are back in archive/"
