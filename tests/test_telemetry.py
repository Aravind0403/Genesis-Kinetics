"""
Unit tests for RTMProbe and FlightRecorder.
"""

import os
import time
import pytest

from telemetry.rtm_probe import RTMProbe, RTMEventType
from telemetry.recorder import FlightRecorder
from telemetry.visualizer import print_flight_summary


def test_flight_recorder_socket_and_parsing():
    sock_path = "/tmp/test_rtm.sock"
    tick_path = "/tmp/test_telemetry.tick"

    recorder = FlightRecorder(socket_path=sock_path, output_path=tick_path)
    recorder.start()

    probe = RTMProbe(socket_path=sock_path, enabled=True)

    try:
        # Give recorder socket a moment to bind
        time.sleep(0.05)

        # Emit events from simulated System 1 and System 2
        probe.record(RTMEventType.CAMERA_INGEST, seq_id=1, latency_us=500)
        probe.record(RTMEventType.VLM_PREFILL, seq_id=1, latency_us=25000)
        probe.record(RTMEventType.CHUNK_GEN, seq_id=1, latency_us=85000)
        probe.record(RTMEventType.MOTOR_TICK, seq_id=1, buffer_depth=45, joint_torque_norm=12.4)
        probe.record(RTMEventType.DEADLINE_MISS, seq_id=2, buffer_depth=0)

        # Wait for recorder to process
        time.sleep(0.15)
        recorder.stop()

        # Parse saved .tick binary file
        loaded = FlightRecorder.load_tick_file(tick_path)
        assert len(loaded) >= 5

        event_types = [r.event_type for r in loaded]
        assert RTMEventType.CAMERA_INGEST in event_types
        assert RTMEventType.CHUNK_GEN in event_types
        assert RTMEventType.DEADLINE_MISS in event_types

        # Verify visualizer runs cleanly
        print_flight_summary(loaded)

    finally:
        probe.close()
        recorder.stop()
        if os.path.exists(tick_path):
            os.unlink(tick_path)
