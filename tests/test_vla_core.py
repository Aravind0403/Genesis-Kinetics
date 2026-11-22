"""
Unit tests for SharedActionRingBuffer and TrajectoryEngine.
"""

import numpy as np
import pytest
import time

from vla_runtime.types import ActionChunkPacket, ControllerState, TrajectoryConfig
from vla_runtime.buffer import SharedActionRingBuffer
from vla_runtime.trajectory import TrajectoryEngine


def test_shared_memory_ring_buffer_produce_consume():
    shm_name = "test_vla_ring_buffer"
    producer = SharedActionRingBuffer(name=shm_name, chunk_horizon=50, joint_dof=7, create=True)
    consumer = SharedActionRingBuffer(name=shm_name, chunk_horizon=50, joint_dof=7, create=False)

    try:
        # Consumer should read None initially
        assert consumer.consume_latest() is None

        # Produce packet 1
        dummy_actions = np.ones((50, 7), dtype=np.float64) * 1.5
        t_obs = time.monotonic_ns()
        packet1 = ActionChunkPacket(
            seq_id=1,
            t_obs_captured_ns=t_obs,
            t_produced_ns=t_obs + 50_000_000,
            actions=dummy_actions,
        )
        assert producer.produce(packet1) is True

        # Consume packet 1
        rec1 = consumer.consume_latest(last_seen_seq=-1)
        assert rec1 is not None
        assert rec1.seq_id == 1
        assert rec1.actions.shape == (50, 7)
        assert np.allclose(rec1.actions, 1.5)

        # Consuming again without new produce should return None
        assert consumer.consume_latest(last_seen_seq=1) is None

        # Produce packet 2
        packet2 = ActionChunkPacket(
            seq_id=2,
            t_obs_captured_ns=t_obs + 100_000_000,
            t_produced_ns=t_obs + 160_000_000,
            actions=dummy_actions * 2.0,
        )
        assert producer.produce(packet2) is True
        rec2 = consumer.consume_latest(last_seen_seq=1)
        assert rec2 is not None
        assert rec2.seq_id == 2
        assert np.allclose(rec2.actions, 3.0)

    finally:
        producer.close()
        consumer.close()
        producer.unlink()


def test_trajectory_engine_temporal_ensembling():
    config = TrajectoryConfig(control_frequency_hz=100.0, chunk_horizon_steps=50, joint_dof=7)
    engine = TrajectoryEngine(config=config)

    t0 = time.monotonic_ns()
    # Chunk 1: all 1.0s
    c1 = ActionChunkPacket(
        seq_id=1,
        t_obs_captured_ns=t0,
        t_produced_ns=t0 + 50_000_000,
        actions=np.ones((50, 7)) * 1.0,
    )
    engine.ingest_chunk(c1, t_now_ns=t0 + 50_000_000)  # 50ms = 5 steps elapsed
    assert engine.state == ControllerState.TRACKING_NOMINAL
    assert len(engine.active_trajectory) == 45  # 50 - 5

    # Step 10 times (100ms)
    q_curr = np.zeros(7)
    qd_curr = np.zeros(7)
    for _ in range(10):
        target, state = engine.step(q_curr, qd_curr)
        assert np.allclose(target, 1.0)
    assert len(engine.active_trajectory) == 35

    # Ingest Chunk 2: all 2.0s captured at t0 + 100ms, arriving at t0 + 160ms (6 steps elapsed)
    t_chunk2_obs = t0 + 100_000_000
    t_chunk2_arr = t0 + 160_000_000
    c2 = ActionChunkPacket(
        seq_id=2,
        t_obs_captured_ns=t_chunk2_obs,
        t_produced_ns=t_chunk2_arr,
        actions=np.ones((50, 7)) * 2.0,
    )
    engine.ingest_chunk(c2, t_now_ns=t_chunk2_arr)
    assert engine.state == ControllerState.ENSEMBLING

    # The next step should be a blended value between 1.0 and 2.0!
    target, state = engine.step(q_curr, qd_curr)
    assert 1.0 < target[0] < 2.0
    print(f"Smoothly blended waypoint value: {target[0]:.4f}")


def test_trajectory_engine_damped_fallback():
    config = TrajectoryConfig(control_frequency_hz=100.0, critical_damping_time_s=0.1)
    engine = TrajectoryEngine(config=config)

    # Empty buffer: immediately triggers damped fallback
    q_curr = np.array([0.5] * 7)
    qd_curr = np.array([1.0] * 7)  # Moving with positive velocity

    target_0, state_0 = engine.step(q_curr, qd_curr)
    assert state_0 == ControllerState.DAMPED_FALLBACK

    # Step until damping finishes (10 steps for 0.1s at 100Hz)
    last_target = target_0
    for _ in range(12):
        target, state = engine.step(last_target, qd_curr)
        last_target = target

    assert state == ControllerState.DAMPED_FALLBACK
    # Velocity stopped, position settled smoothly without explosion
    assert np.all(np.isfinite(last_target))


def test_genesis_arm_env_step():
    from vla_runtime.genesis_env import GenesisArmEnv
    env = GenesisArmEnv(show_viewer=False, control_freq_hz=100.0)
    s0 = env.reset()
    assert s0.joint_positions.shape == (7,)
    assert s0.joint_velocities.shape == (7,)

    # Step toward a perturbed joint target
    target = s0.joint_positions + 0.02
    s1 = env.step_control(target)
    assert s1.joint_positions.shape == (7,)
    assert not np.isnan(s1.joint_positions).any()

