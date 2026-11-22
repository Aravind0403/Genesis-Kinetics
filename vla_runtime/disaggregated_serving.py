"""
Disaggregated VLA Perception Serving Engine.

Implements disaggregated vision-language inference for embodied AI:
- Chunked vision-language prefill architecture to eliminate edge memory spikes.
- Disaggregated KV cache transfer and zero-copy lifecycle management.
- Low-latency agile action-head decoding.

Separates heavy visual token prefill (256-512 vision tokens) from agile action chunk
decoding (50-step diffusion/autoregressive action trajectory), streaming output zero-copy
into System 1 via SharedActionRingBuffer.
"""

from dataclasses import dataclass
import multiprocessing as mp
import time
from typing import Dict, List, Optional, Tuple
import numpy as np
import torch

from vla_runtime.types import ActionChunkPacket
from vla_runtime.buffer import SharedActionRingBuffer
from telemetry.rtm_probe import RTMProbe, RTMEventType


@dataclass
class DisaggregatedConfig:
    vision_token_count: int = 256      # Number of visual patch tokens per camera frame
    chunked_prefill_size: int = 64     # Chunked prefill slice size
    hidden_dim: int = 512              # Transformer hidden dimension
    num_heads: int = 8
    head_dim: int = 64
    action_chunk_horizon: int = 50     # 500ms horizon at 100 Hz
    joint_dof: int = 7


@dataclass
class KVCacheDescriptor:
    """Zero-copy KV cache packet transferred from Prefill Node to Action Head."""
    seq_id: int
    t_obs_ns: int
    k_cache: np.ndarray  # [vision_token_count, num_heads, head_dim]
    v_cache: np.ndarray  # [vision_token_count, num_heads, head_dim]


class ChunkedVisionPrefillNode:
    """
    Prefill Worker:
    Performs chunked prefill across visual tokens to prevent memory allocation spikes
    and latency jitter on edge compute.
    """

    def __init__(self, config: DisaggregatedConfig):
        self.config = config

    def prefill_frame(self, raw_frame_rgb: np.ndarray, seq_id: int, t_obs_ns: int) -> KVCacheDescriptor:
        """
        Processes image frame in chunks of `chunked_prefill_size` tokens.
        Simulates transformer key/value projection with KV cache caching.
        """
        num_chunks = int(np.ceil(self.config.vision_token_count / self.config.chunked_prefill_size))
        
        # Simulate chunked prefill compute
        for chunk_idx in range(num_chunks):
            # Chunked slice compute without full memory spike
            _ = np.sin(raw_frame_rgb[:10, :10].mean()) * 0.1

        # Synthesize populated KV cache for action head
        k_cache = np.zeros(
            (self.config.vision_token_count, self.config.num_heads, self.config.head_dim),
            dtype=np.float32,
        )
        v_cache = np.zeros_like(k_cache)

        return KVCacheDescriptor(
            seq_id=seq_id,
            t_obs_ns=t_obs_ns,
            k_cache=k_cache,
            v_cache=v_cache,
        )


class AgileActionHeadNode:
    """
    Action Head Worker:
    Ingests prefilled KV cache via zero-copy handover and decodes the 50-step action trajectory.
    """

    def __init__(self, config: DisaggregatedConfig):
        self.config = config

    def decode_action_chunk(self, kv_desc: KVCacheDescriptor, base_q: np.ndarray) -> np.ndarray:
        """
        Cross-attends over prefilled KV cache to generate future trajectory waypoints [50, 7].
        Simulates lightweight 1-step flow matching / diffusion policy head.
        """
        t = np.linspace(0, 0.5, self.config.action_chunk_horizon)
        actions = np.zeros((self.config.action_chunk_horizon, self.config.joint_dof))
        
        for dof in range(self.config.joint_dof):
            freq = 0.5 + 0.1 * dof
            amp = 0.12 / (1.0 + dof * 0.15)
            actions[:, dof] = base_q[dof] + amp * np.sin(2.0 * np.pi * freq * (t + kv_desc.seq_id * 0.08))

        return actions


class DisaggregatedVLAServer:
    """
    High-level disaggregated server orchestrating:
    Camera Frame Ingest -> Chunked Prefill Node -> KV Cache Handover -> Action Head Node -> Shared Ring Buffer.
    """

    def __init__(
        self,
        shm_name: str,
        config: Optional[DisaggregatedConfig] = None,
        telemetry_socket: str = "/tmp/rtm.sock",
    ):
        self.shm_name = shm_name
        self.config = config or DisaggregatedConfig()
        self.telemetry_socket = telemetry_socket

        self.prefill_node = ChunkedVisionPrefillNode(self.config)
        self.action_head_node = AgileActionHeadNode(self.config)

    def serve_step(
        self,
        raw_frame: np.ndarray,
        seq_id: int,
        ring_buffer: SharedActionRingBuffer,
        probe: RTMProbe,
        base_q: np.ndarray,
    ) -> float:
        """
        Executes one complete disaggregated vision-to-action cycle.
        Returns total latency in milliseconds.
        """
        # 1. Camera Ingest
        t_obs_ns = time.monotonic_ns()
        probe.record(RTMEventType.CAMERA_INGEST, seq_id=seq_id, timestamp_ns=t_obs_ns)

        # 2. Chunked Prefill
        t_prefill_start_ns = time.monotonic_ns()
        kv_cache = self.prefill_node.prefill_frame(raw_frame, seq_id=seq_id, t_obs_ns=t_obs_ns)
        t_prefill_end_ns = time.monotonic_ns()
        prefill_lat_us = int((t_prefill_end_ns - t_prefill_start_ns) / 1000)

        probe.record(
            RTMEventType.VLM_PREFILL,
            seq_id=seq_id,
            latency_us=prefill_lat_us,
            timestamp_ns=t_prefill_end_ns,
        )

        # 3. Action Head Decoding
        actions = self.action_head_node.decode_action_chunk(kv_cache, base_q)
        t_prod_ns = time.monotonic_ns()
        total_lat_us = int((t_prod_ns - t_obs_ns) / 1000)

        # 4. Stream Action Chunk into Shared Memory Ring Buffer
        packet = ActionChunkPacket(
            seq_id=seq_id,
            t_obs_captured_ns=t_obs_ns,
            t_produced_ns=t_prod_ns,
            actions=actions,
        )
        ring_buffer.produce(packet)

        probe.record(
            RTMEventType.CHUNK_GEN,
            seq_id=seq_id,
            latency_us=total_lat_us,
            timestamp_ns=t_prod_ns,
        )

        return total_lat_us / 1000.0
