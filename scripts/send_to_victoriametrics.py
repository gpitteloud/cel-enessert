#!/usr/bin/env python3
"""
Direct VictoriaMetrics sender for CEL data

Sends parsed SDAT data directly to VictoriaMetrics.
Expects data points in VictoriaMetrics NDJSON format.
"""

import requests
import logging
import json
from typing import List, Dict

logger = logging.getLogger(__name__)


def send_to_victoriametrics(data_points: List[Dict], vm_url: str = "http://localhost:8428"):
    """
    Send data points directly to VictoriaMetrics import API

    Expected format (VictoriaMetrics NDJSON):
    {
        "metric": {
            "__name__": "metric_name",
            "label1": "value1",
            "label2": "value2"
        },
        "values": [value],
        "timestamps": [timestamp_ms]
    }
    """

    if not data_points:
        logger.warning("No data points to send")
        return False

    # Validate format
    vm_metrics = []
    for point in data_points:
        # All data points should already be in VictoriaMetrics format
        if 'metric' not in point or 'values' not in point or 'timestamps' not in point:
            logger.error(f"Invalid data point format: {point}")
            continue
        vm_metrics.append(point)

    if not vm_metrics:
        logger.warning("No valid data points after validation")
        return False

    # Send to VictoriaMetrics
    # VM /api/v1/import expects newline-delimited JSON (NDJSON), not a JSON array
    import_url = f"{vm_url}/api/v1/import"

    try:
        # Convert to newline-delimited JSON
        ndjson_data = '\n'.join(json.dumps(metric) for metric in vm_metrics)

        response = requests.post(
            import_url,
            data=ndjson_data,
            headers={'Content-Type': 'application/json'},
            timeout=30
        )

        if response.status_code == 204:
            logger.debug(f"Successfully sent {len(vm_metrics)} metrics to VictoriaMetrics")
            return True
        else:
            logger.error(f"VictoriaMetrics returned status {response.status_code}: {response.text}")
            return False

    except requests.exceptions.Timeout:
        logger.error("Timeout sending data to VictoriaMetrics")
        return False
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error sending to VictoriaMetrics: {e}")
        return False
    except Exception as e:
        logger.error(f"Error sending data to VictoriaMetrics: {e}")
        return False


def send_batch(data_points: List[Dict], vm_url: str = "http://localhost:8428", batch_size: int = 1000):
    """
    Send data in batches to avoid overwhelming VictoriaMetrics
    """
    success_count = 0
    error_count = 0

    for i in range(0, len(data_points), batch_size):
        batch = data_points[i:i + batch_size]
        if send_to_victoriametrics(batch, vm_url):
            success_count += len(batch)
        else:
            error_count += len(batch)

    logger.info(f"Sent {success_count} data points successfully, {error_count} errors")
    return success_count, error_count
