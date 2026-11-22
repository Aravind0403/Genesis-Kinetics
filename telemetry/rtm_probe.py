"""
Microsecond-level telemetry probe for real-time-monitor (RTM) integration.
Transmits structured metrics over a non-blocking Unix Domain Socket (/tmp/rtm.sock)
or memory buffer to record latency, buffer depth, deadline misses, and physics impulses.
"""

from enum import IntEnum
import os
import socket
import struct
import time
from typing import Optional


class RTMEventType(IntEnum):
    CAMERA_INGEST = 1       # Camera frame sampled from sim/hardware
    VLM_PREFILL = 2         # System 2 prefill started
    CHUNK_GEN = 3           # System 2 action chunk generation finished
    IPC_HANDOVER = 4        # Chunk read by System 1 across shared memory
    MOTOR_TICK = 5          # System 1 100 Hz step completed
    DEADLINE_MISS = 6       # Buffer starved; entered damped fallback
    CONTACT_IMPULSE = 7     # Physics engine detected object contact


# Binary Record Struct for .tick format (36 bytes per event):
# [timestamp_ns (8B uint64), event_type (1B uint8), seq_id (8B uint64),
#  latency_us (4B uint32), buffer_depth (4B uint32),
#  joint_torque_norm (4B float32), contact_force_norm (4B float32), reserved (3B)]
TICK_RECORD_STRUCT = struct.Struct("<QBQIIff3s")
TICK_RECORD_SIZE = TICK_RECORD_STRUCT.size


class RTMProbe:
    """
    Non-blocking telemetry client embedded in System 1 and System 2.
    Uses UDP or non-blocking Unix Domain DGRAM sockets to guarantee zero IPC stalls.
    """

    def __init__(self, socket_path: str = "/tmp/rtm.sock", enabled: bool = True):
        self.socket_path = socket_path
        self.enabled = enabled
        self.sock: Optional[socket.socket] = None
        if self.enabled:
            self._connect()

    def _connect(self):
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.setblocking(False)
        except Exception:
            self.sock = None

    def record(
        self,
        event_type: RTMEventType,
        seq_id: int = 0,
        latency_us: int = 0,
        buffer_depth: int = 0,
        joint_torque_norm: float = 0.0,
        contact_force_norm: float = 0.0,
        timestamp_ns: Optional[int] = None,
    ):
        if not self.enabled or self.sock is None:
            return

        ts_ns = timestamp_ns if timestamp_ns is not None else time.monotonic_ns()
        safe_seq_id = max(0, seq_id)
        payload = TICK_RECORD_STRUCT.pack(
            ts_ns,
            int(event_type),
            safe_seq_id,
            latency_us,
            buffer_depth,
            float(joint_torque_norm),
            float(contact_force_norm),
            b"\x00\x00\x00",
        )

        try:
            self.sock.sendto(payload, self.socket_path)
        except (BlockingIOError, ConnectionRefusedError, FileNotFoundError, OSError):
            # Best-effort telemetry: never crash the real-time motor loop
            pass

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
