"""
End-to-End Disaggregated VLA Serving Demonstration.

Connects:
1. Visual Observation Ingestion (Genesis World Tabletop Scene)
2. Chunked Vision-Language Prefill Node
3. Zero-Copy KV Cache Transfer
4. Agile Low-Latency Action Head
5. Zero-Copy Shared Ring Buffer -> 100 Hz System 1 Motor Controller
6. Closed-Loop Flight Recorder (.tick) Telemetry
"""

import os
import sys
import time
from typing import Dict, List
import numpy as np

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vla_runtime.types import TrajectoryConfig
from vla_runtime.buffer import SharedActionRingBuffer
from vla_runtime.trajectory import TrajectoryEngine
from vla_runtime.genesis_env import GenesisArmEnv
from vla_runtime.disaggregated_serving import DisaggregatedVLAServer, DisaggregatedConfig
from telemetry.recorder import FlightRecorder
from telemetry.rtm_probe import RTMProbe, RTMEventType
from telemetry.visualizer import print_flight_summary


def main():
    print("=" * 84)
    print(" 🚀 DISAGGREGATED VLA SERVING ARCHITECTURE DEMONSTRATION")
    print("=" * 84)
    print("Architecture: Chunked Vision Prefill + Zero-Copy KV Cache Transfer")
    print("              + Agile Fused Action Head + Genesis World 100 Hz Motor Loop\n")

    shm_name = f"shm_disagg_{int(time.time()*1000)%100000}"
    sock_path = "/tmp/rtm_disaggregated.sock"
    tick_path = "disaggregated_vla_flight.tick"

    # Start Flight Recorder
    recorder = FlightRecorder(socket_path=sock_path, output_path=tick_path)
    recorder.start()
    probe = RTMProbe(socket_path=sock_path, enabled=True)

    # 1. Initialize Ring Buffer
    ring_buffer = SharedActionRingBuffer(name=shm_name, chunk_horizon=50, joint_dof=7, create=True)

    # 2. Initialize Disaggregated Serving Engine
    disagg_config = DisaggregatedConfig(
        vision_token_count=256,
        chunked_prefill_size=64,
        action_chunk_horizon=50,
        joint_dof=7,
    )
    vla_server = DisaggregatedVLAServer(
        shm_name=shm_name,
        config=disagg_config,
        telemetry_socket=sock_path,
    )

    # 3. Initialize Genesis World Tabletop Environment
    print("[1/3] Initializing Genesis World Tabletop Simulation...")
    env = GenesisArmEnv(show_viewer=False, control_freq_hz=100.0)
    state = env.reset()

    # 4. Initialize System 1 100 Hz Trajectory Engine
    trajectory_engine = TrajectoryEngine(config=TrajectoryConfig(control_frequency_hz=100.0))

    print("[2/3] Streaming Disaggregated VLA Inference into 100 Hz Closed-Loop Motor Control...")
    total_ticks = 150  # 1.5 seconds at 100 Hz
    dt = 0.01          # 10 ms cycle
    next_deadline_ns = time.monotonic_ns()
    latencies = []

    last_seq_id = 0

    try:
        for tick in range(total_ticks):
            now_ns = time.monotonic_ns()

            # Trigger Disaggregated System 2 Step periodically (at 5 Hz = every 20 ticks)
            if tick % 20 == 0:
                seq_id = (tick // 20) + 1
                # Synthesize / sample camera frame (480x640x3 RGB)
                raw_frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)

                # Serve disaggregated step
                step_lat_ms = vla_server.serve_step(
                    raw_frame=raw_frame,
                    seq_id=seq_id,
                    ring_buffer=ring_buffer,
                    probe=probe,
                    base_q=state.joint_positions,
                )
                latencies.append(step_lat_ms)

            # System 1 Consumes Latest Chunk from Ring Buffer
            new_chunk = ring_buffer.consume_latest(last_seen_seq=last_seq_id)
            if new_chunk is not None:
                last_seq_id = new_chunk.seq_id
                probe.record(RTMEventType.IPC_HANDOVER, seq_id=new_chunk.seq_id, timestamp_ns=now_ns)
                trajectory_engine.ingest_chunk(new_chunk, t_now_ns=now_ns)

            # System 1 100 Hz Control Step
            target_q, c_state = trajectory_engine.step(state.joint_positions, state.joint_velocities)
            state = env.step_control(target_q=target_q)

            # Telemetry
            buf_depth = len(trajectory_engine.active_trajectory) if trajectory_engine.active_trajectory is not None else 0
            probe.record(
                RTMEventType.MOTOR_TICK,
                seq_id=last_seq_id,
                buffer_depth=buf_depth,
                joint_torque_norm=float(np.linalg.norm(state.applied_torques)),
                timestamp_ns=now_ns,
            )

            # Clock pacing
            next_deadline_ns += int(dt * 1e9)
            rem = next_deadline_ns - time.monotonic_ns()
            if rem > 100_000:
                time.sleep(rem / 1e9)

    finally:
        ring_buffer.set_shutdown()
        ring_buffer.close()
        ring_buffer.unlink()
        recorder.stop()
        probe.close()

    print("[3/3] Profiling Disaggregated Serving Performance...")
    print("\n" + "=" * 84)
    print(" 📊 DISAGGREGATED SERVING BENCHMARK REPORT")
    print("=" * 84)
    print(f"Total Disaggregated Perception Cycles : {len(latencies)}")
    print(f"Chunked Prefill Batch Size           : {disagg_config.chunked_prefill_size} tokens / slice")
    print(f"Visual Token Horizon                  : {disagg_config.vision_token_count} vision tokens")
    print(f"Action Chunk Trajectory Horizon       : {disagg_config.action_chunk_horizon} steps (500 ms)")
    print(f"Average Disaggregated Serving Latency : {np.mean(latencies):.2f} ms")
    print(f"P95 Disaggregated Serving Latency     : {np.percentile(latencies, 95):.2f} ms")
    print("=" * 84)

    records = FlightRecorder.load_tick_file(tick_path)
    print_flight_summary(records)


if __name__ == "__main__":
    main()
