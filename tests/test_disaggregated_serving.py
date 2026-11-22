"""
Unit tests for the Disaggregated VLA Serving Engine.
"""

import numpy as np
import pytest

from vla_runtime.disaggregated_serving import (
    DisaggregatedConfig,
    DisaggregatedVLAServer,
    ChunkedVisionPrefillNode,
    AgileActionHeadNode,
)
from vla_runtime.buffer import SharedActionRingBuffer
from telemetry.rtm_probe import RTMProbe


def test_chunked_prefill_and_action_head():
    config = DisaggregatedConfig(
        vision_token_count=128,
        chunked_prefill_size=32,
        action_chunk_horizon=50,
        joint_dof=7,
    )
    prefill_node = ChunkedVisionPrefillNode(config)
    action_node = AgileActionHeadNode(config)

    # Fake RGB frame (640x480x3)
    dummy_frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)

    # 1. Prefill
    kv_desc = prefill_node.prefill_frame(dummy_frame, seq_id=1, t_obs_ns=1000)
    assert kv_desc.seq_id == 1
    assert kv_desc.k_cache.shape == (128, 8, 64)

    # 2. Action head decode
    base_q = np.zeros(7)
    actions = action_node.decode_action_chunk(kv_desc, base_q)
    assert actions.shape == (50, 7)
    assert not np.isnan(actions).any()


def test_disaggregated_server_end_to_end():
    shm_name = "test_disaggregated_shm"
    ring_buf = SharedActionRingBuffer(name=shm_name, chunk_horizon=50, joint_dof=7, create=True)
    probe = RTMProbe(enabled=False)

    try:
        server = DisaggregatedVLAServer(shm_name=shm_name)
        dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        base_q = np.array([0.0, -0.785, 0.0, -2.356, 0.0, 1.571, 0.785])

        latency_ms = server.serve_step(
            raw_frame=dummy_frame,
            seq_id=1,
            ring_buffer=ring_buf,
            probe=probe,
            base_q=base_q,
        )
        assert latency_ms >= 0.0

        # Verify ring buffer received valid chunk
        packet = ring_buf.consume_latest()
        assert packet is not None
        assert packet.seq_id == 1
        assert packet.actions.shape == (50, 7)
    finally:
        ring_buf.close()
        ring_buf.unlink()
