"""
High-performance background flight recorder daemon.
Listens on /tmp/rtm.sock and streams incoming telemetry records into a compact .tick binary file.
Also provides parsing utilities for offline replay, analysis, and dashboard visualization.
"""

from dataclasses import dataclass
import os
import socket
import threading
import time
from typing import List, Optional
import numpy as np

from telemetry.rtm_probe import (
    RTMEventType,
    TICK_RECORD_STRUCT,
    TICK_RECORD_SIZE,
)


@dataclass
class TickRecord:
    timestamp_ns: int
    event_type: RTMEventType
    seq_id: int
    latency_us: int
    buffer_depth: int
    joint_torque_norm: float
    contact_force_norm: float


class FlightRecorder:
    """
    Flight recorder server that captures telemetry from /tmp/rtm.sock and writes to disk.
    """

    def __init__(self, socket_path: str = "/tmp/rtm.sock", output_path: str = "run_telemetry.tick"):
        self.socket_path = socket_path
        self.output_path = output_path
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.records: List[TickRecord] = []

    def start(self):
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.socket_path)
        self.sock.settimeout(0.1)
        self.running = True
        self.records.clear()

        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def _listen_loop(self):
        with open(self.output_path, "wb") as f:
            while self.running:
                try:
                    data, _ = self.sock.recvfrom(2048)
                    if len(data) >= TICK_RECORD_SIZE:
                        # Write raw bytes directly to .tick file for zero serialization overhead
                        f.write(data)
                        f.flush()
                        
                        # Also cache record for in-memory analysis
                        unpacked = TICK_RECORD_STRUCT.unpack(data[:TICK_RECORD_SIZE])
                        rec = TickRecord(
                            timestamp_ns=unpacked[0],
                            event_type=RTMEventType(unpacked[1]),
                            seq_id=unpacked[2],
                            latency_us=unpacked[3],
                            buffer_depth=unpacked[4],
                            joint_torque_norm=unpacked[5],
                            contact_force_norm=unpacked[6],
                        )
                        self.records.append(rec)
                except socket.timeout:
                    continue
                except OSError:
                    break

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        try:
            self.sock.close()
        except Exception:
            pass
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    @staticmethod
    def load_tick_file(file_path: str) -> List[TickRecord]:
        """Loads and parses a .tick binary file into structured records."""
        records = []
        if not os.path.exists(file_path):
            return records

        with open(file_path, "rb") as f:
            while True:
                buf = f.read(TICK_RECORD_SIZE)
                if len(buf) < TICK_RECORD_SIZE:
                    break
                unpacked = TICK_RECORD_STRUCT.unpack(buf)
                records.append(
                    TickRecord(
                        timestamp_ns=unpacked[0],
                        event_type=RTMEventType(unpacked[1]),
                        seq_id=unpacked[2],
                        latency_us=unpacked[3],
                        buffer_depth=unpacked[4],
                        joint_torque_norm=unpacked[5],
                        contact_force_norm=unpacked[6],
                    )
                )
        return records
