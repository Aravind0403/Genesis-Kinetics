"""
System 2 VLA Perception Server.
Runs asynchronously at 5-10 Hz in an independent process, generating action chunks
and streaming them zero-copy into the SharedActionRingBuffer.
"""

import multiprocessing as mp
import os
import time
import numpy as np
from typing import Optional

from vla_runtime.types import ActionChunkPacket
from vla_runtime.buffer import SharedActionRingBuffer
from telemetry.rtm_probe import RTMProbe, RTMEventType
from experiments.latency_profiles import LatencyProfile, LatencyMode


class System2Worker(mp.Process):
    """
    Asynchronous VLM Perception Worker.
    In a full deployment, this runs OpenVLA, π0, or an SGLang/vLLM inference server.
    Here, it executes high-fidelity trajectory chunk synthesis with configurable latency profiles.
    """

    def __init__(
        self,
        shm_name: str,
        chunk_horizon: int = 50,
        joint_dof: int = 7,
        latency_profile: Optional[LatencyProfile] = None,
        telemetry_socket: str = "/tmp/rtm.sock",
        max_chunks: int = 50,
    ):
        super().__init__(name="System2-VLM-Worker")
        self.shm_name = shm_name
        self.chunk_horizon = chunk_horizon
        self.joint_dof = joint_dof
        self.latency_profile = latency_profile or LatencyProfile()
        self.telemetry_socket = telemetry_socket
        self.max_chunks = max_chunks

    def _generate_synthetic_policy_chunk(self, seq_id: int, base_q: np.ndarray) -> np.ndarray:
        """
        Synthesizes a realistic smooth reaching action chunk (50 steps x 7 DOF).
        Simulates the output of a diffusion policy predicting future waypoints.
        """
        t = np.linspace(0, 0.5, self.chunk_horizon)  # 500 ms horizon
        chunk = np.zeros((self.chunk_horizon, self.joint_dof))
        for dof in range(self.joint_dof):
            freq = 0.5 + 0.1 * dof
            amp = 0.15 / (1.0 + dof * 0.2)
            chunk[:, dof] = base_q[dof] + amp * np.sin(2.0 * np.pi * freq * (t + seq_id * 0.1))
        return chunk

    def run(self):
        # Attach to existing shared memory ring buffer
        ring_buffer = SharedActionRingBuffer(
            name=self.shm_name,
            chunk_horizon=self.chunk_horizon,
            joint_dof=self.joint_dof,
            create=False,
        )
        probe = RTMProbe(socket_path=self.telemetry_socket, enabled=True)

        seq_id = 0
        base_q = np.array([0.0, -0.785, 0.0, -2.356, 0.0, 1.571, 0.785])  # Franka ready pose

        try:
            while seq_id < self.max_chunks and not ring_buffer.is_shutdown():
                seq_id += 1
                
                # 1. Camera Ingestion Timestamp
                t_obs_captured_ns = time.monotonic_ns()
                probe.record(RTMEventType.CAMERA_INGEST, seq_id=seq_id, timestamp_ns=t_obs_captured_ns)

                # 2. Simulate VLM Prefill & Diffusion Latency
                latency_s = self.latency_profile.sample_latency_s(seq_id)
                prefill_latency_s = latency_s * 0.3
                decode_latency_s = latency_s * 0.7

                # Prefill telemetry
                probe.record(
                    RTMEventType.VLM_PREFILL,
                    seq_id=seq_id,
                    latency_us=int(prefill_latency_s * 1e6),
                )
                time.sleep(prefill_latency_s)

                # Chunk generation
                raw_chunk = self._generate_synthetic_policy_chunk(seq_id, base_q)
                time.sleep(decode_latency_s)

                t_produced_ns = time.monotonic_ns()
                total_latency_us = int((t_produced_ns - t_obs_captured_ns) / 1000)

                # 3. Hand over chunk into lock-free ring buffer
                packet = ActionChunkPacket(
                    seq_id=seq_id,
                    t_obs_captured_ns=t_obs_captured_ns,
                    t_produced_ns=t_produced_ns,
                    actions=raw_chunk,
                )
                ring_buffer.produce(packet)

                # Telemetry record for chunk generation
                probe.record(
                    RTMEventType.CHUNK_GEN,
                    seq_id=seq_id,
                    latency_us=total_latency_us,
                )

        finally:
            probe.close()
            ring_buffer.close()
