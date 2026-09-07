#!/usr/bin/env python3

import csv
import subprocess
import time
from datetime import datetime, timezone
import httpx

# Configuration
# Middle Y is the proxy decision entry-point: it forwards to Cloud X when the
# RTT is low, and falls back to local execution when latency degrades.
MIDDLE_Y_URL = "http://172.20.0.20:8001/execute"
# Containers used for link-based netem delay injection
TARGET_CONTAINERS = ["end_z", "middle_y", "cloud_x"]
OUTPUT_CSV = "benchmark_results.csv"
REQUEST_INTERVAL_SEC = 0.5  # Time between samples

# Link-based delays: Z↔Y split on end_z+middle_y, Y↔X split on middle_y+cloud_x.
# Each phase sets (zy_delay, yx_delay) in ms.
TEST_PHASES = [
    ("Baseline (No Delay)", 0, 0, 15),
    ("Moderate Z↔Y (10ms)", 10, 0, 15),
    ("Moderate Y↔X (20ms)", 0, 20, 15),
    ("Both Degraded (30ms + 40ms)", 30, 40, 20),
    ("Recovery (Cleared)", 0, 0, 15),
]


def apply_link_delays(zy_ms: int, yx_ms: int) -> None:
    """Apply link-based netem delays split between endpoints."""
    for container, delay in [
        ("end_z", zy_ms // 2),
        ("middle_y", (zy_ms // 2) + (yx_ms // 2)),
        ("cloud_x", yx_ms // 2),
    ]:
        if delay == 0:
            cmd = f"docker exec {container} tc qdisc del dev eth0 root"
        else:
            cmd = (
                f"docker exec {container} tc qdisc del dev eth0 root 2>/dev/null; "
                f"docker exec {container} tc qdisc add dev eth0 root netem delay {delay}ms"
            )
        subprocess.run(cmd, shell=True, capture_output=True, text=True)


def run_benchmark() -> None:
    print(f"Starting Benchmark Harness... Output file: {OUTPUT_CSV}")
    
    # Initialize CSV file
    with open(OUTPUT_CSV, mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow([
            "timestamp",
            "phase",
            "status_code",
            "total_rtt_ms",
            "executed_by",
            "error"
        ])

        # Always start with clean network state
        apply_link_delays(0, 0)

        client = httpx.Client(timeout=5.0)

        try:
            for phase_name, zy_delay, yx_delay, duration in TEST_PHASES:
                print(f"\n--- Entering Phase: {phase_name} ({duration}s) ---")
                apply_link_delays(zy_delay, yx_delay)

                end_time = time.time() + duration
                while time.time() < end_time:
                    now_str = datetime.now(timezone.utc).isoformat()
                    start_ts = time.perf_counter()
                    
                    status_code = None
                    executed_by = "UNKNOWN"
                    error_msg = ""

                    try:
                        payload = {
                            "request_id": f"bench_{int(time.time() * 1000)}",
                            "source": {
                                "tier": "end_z",
                                "node_id": "end_z",
                                "host": "172.20.0.30",
                                "port": 8002,
                            },
                            "inputs": {"input_id": 1001, "payload": "game_state"},
                            "timestamp_ms": int(time.time() * 1000),
                        }
                        resp = client.post(MIDDLE_Y_URL, json=payload)
                        elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
                        status_code = resp.status_code
                        
                        if resp.status_code == 200:
                            data = resp.json()
                            executed_by = data.get("executed_by", "UNKNOWN")
                        else:
                            error_msg = f"HTTP {resp.status_code}"
                    except Exception as exc:
                        elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
                        error_msg = type(exc).__name__

                    # Write measurement row
                    writer.writerow([
                        now_str,
                        phase_name,
                        status_code,
                        elapsed_ms,
                        executed_by,
                        error_msg
                    ])
                    csv_file.flush()

                    print(
                        f"[{now_str}] Phase: {phase_name:<25} | "
                        f"RTT: {elapsed_ms:>6.2f}ms | ExecutedBy: {executed_by:<15} | "
                        f"Status: {status_code or error_msg}"
                    )

                    time.sleep(REQUEST_INTERVAL_SEC)

        finally:
            print("\nCleaning up network rules...")
            apply_link_delays(0, 0)
            client.close()
            print(f"Benchmark completed. Data saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    run_benchmark()