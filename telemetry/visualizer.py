"""
ASCII & Terminal Inspector for real-time telemetry analysis.
Renders latency histograms, buffer starvation events, and causal failure correlations.
"""

from typing import List
import numpy as np
from telemetry.recorder import TickRecord
from telemetry.rtm_probe import RTMEventType


def print_flight_summary(records: List[TickRecord]):
    """Prints a clear diagnostic summary of recorded flight telemetry."""
    if not records:
        print("[FlightRecorder] No records captured.")
        return

    motor_ticks = [r for r in records if r.event_type == RTMEventType.MOTOR_TICK]
    vlm_chunks = [r for r in records if r.event_type == RTMEventType.CHUNK_GEN]
    deadline_misses = [r for r in records if r.event_type == RTMEventType.DEADLINE_MISS]

    print("\n" + "=" * 70)
    print(" 🚀 FLIGHT RECORDER OBSERVABILITY REPORT")
    print("=" * 70)
    print(f"Total Telemetry Events Logged : {len(records):,}")
    print(f"System 1 Motor Ticks (100 Hz) : {len(motor_ticks):,}")
    print(f"System 2 VLM Chunks Produced  : {len(vlm_chunks):,}")
    print(f"Action Buffer Starvation Misses: {len(deadline_misses):,}")

    if vlm_chunks:
        latencies_ms = [r.latency_us / 1000.0 for r in vlm_chunks]
        p50 = np.percentile(latencies_ms, 50)
        p95 = np.percentile(latencies_ms, 95)
        p99 = np.percentile(latencies_ms, 99)
        max_lat = np.max(latencies_ms)
        print("\n--- System 2 VLM Inference Latency Profile ---")
        print(f"  P50 Latency : {p50:6.1f} ms")
        print(f"  P95 Latency : {p95:6.1f} ms")
        print(f"  P99 Latency : {p99:6.1f} ms")
        print(f"  Max Latency : {max_lat:6.1f} ms")

    if motor_ticks:
        buffer_depths = [r.buffer_depth for r in motor_ticks]
        avg_depth = np.mean(buffer_depths)
        min_depth = np.min(buffer_depths)
        print("\n--- System 1 Action Buffer Health ---")
        print(f"  Avg Buffer Depth : {avg_depth:4.1f} steps")
        print(f"  Min Buffer Depth : {min_depth:4d} steps")
        starvation_rate = (len(deadline_misses) / max(1, len(motor_ticks))) * 100.0
        print(f"  Deadline Miss Rate: {starvation_rate:5.2f} %")

    print("=" * 70 + "\n")
