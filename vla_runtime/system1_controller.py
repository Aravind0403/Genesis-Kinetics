"""
System 1 Motor Controller (100 Hz).
Executes high-frequency control, consuming chunks from the lock-free ring buffer,
performing zero-copy DLPack / NumPy tensor streaming, applying exponential ensembling,
and triggering damped continuation when deadlines are missed.
"""

from dataclasses import dataclass
import math
import time
from typing import List, Optional, Tuple
import numpy as np

from vla_runtime.types import ControllerState, TrajectoryConfig
from vla_runtime.buffer import SharedActionRingBuffer
from vla_runtime.trajectory import TrajectoryEngine
from vla_runtime.genesis_env import GenesisArmEnv
from telemetry.rtm_probe import RTMProbe, RTMEventType


@dataclass
class ControllerTickResult:
    tick_idx: int
    t_now_ns: int
    target_q: np.ndarray
    current_q: np.ndarray
    current_qd: np.ndarray
    state: ControllerState
    buffer_depth: int
    cycle_time_us: int
    torque_norm: float


class System1Controller:
    """
    100 Hz Real-Time Motor Controller.
    Can drive either Genesis World physics or a high-fidelity internal 7-DOF dynamic arm.
    """

    def __init__(
        self,
        shm_name: str,
        config: Optional[TrajectoryConfig] = None,
        telemetry_socket: str = "/tmp/rtm.sock",
        is_asynchronous: bool = True,  # True = Async + Ensembling; False = Synchronous Freeze Baseline
        use_genesis: bool = False,
        show_viewer: bool = False,
    ):
        self.shm_name = shm_name
        self.config = config or TrajectoryConfig()
        self.telemetry_socket = telemetry_socket
        self.is_asynchronous = is_asynchronous
        self.use_genesis = use_genesis

        self.engine = TrajectoryEngine(config=self.config)
        self.dt = 1.0 / self.config.control_frequency_hz
        self.last_seq_id = 0

        # Physical Environment (Genesis World or Dynamic Integration)
        self.env = GenesisArmEnv(show_viewer=show_viewer, control_freq_hz=self.config.control_frequency_hz) if use_genesis else None

        # Physical Robot State (Franka Panda 7-DOF)
        self.q = np.array([0.0, -0.785, 0.0, -2.356, 0.0, 1.571, 0.785])
        self.qd = np.zeros(self.config.joint_dof)
        
        # PD Controller Gains
        self.kp = 250.0  # Stiffness
        self.kd = 25.0   # Damping

    def run_loop(
        self,
        duration_s: float = 2.0,
        ring_buffer: Optional[SharedActionRingBuffer] = None,
    ) -> List[ControllerTickResult]:
        """
        Runs the 100 Hz loop for duration_s seconds, strictly paced by monotonic clock.
        """
        probe = RTMProbe(socket_path=self.telemetry_socket, enabled=True)
        results: List[ControllerTickResult] = []

        total_ticks = int(duration_s * self.config.control_frequency_hz)
        target_cycle_ns = int(self.dt * 1e9)  # 10,000,000 ns = 10 ms

        loop_start_ns = time.monotonic_ns()
        next_deadline_ns = loop_start_ns

        for tick in range(total_ticks):
            cycle_start_ns = time.monotonic_ns()

            # 1. Ingest latest chunk from shared memory if available
            if ring_buffer is not None:
                new_chunk = ring_buffer.consume_latest(last_seen_seq=self.last_seq_id)
                if new_chunk is not None:
                    self.last_seq_id = new_chunk.seq_id
                    probe.record(
                        RTMEventType.IPC_HANDOVER,
                        seq_id=new_chunk.seq_id,
                        timestamp_ns=cycle_start_ns,
                    )
                    if self.is_asynchronous:
                        # Ingest into trajectory engine for temporal ensembling
                        self.engine.ingest_chunk(new_chunk, t_now_ns=cycle_start_ns)
                    else:
                        # SYNCHRONOUS BASELINE: overwrite directly, no ensembling or lag compensation
                        self.engine.active_trajectory = new_chunk.actions.copy()
                        self.engine.state = ControllerState.TRACKING_NOMINAL

            # 2. Get target waypoint for this 10ms cycle
            buffer_depth = len(self.engine.active_trajectory) if self.engine.active_trajectory is not None else 0
            
            if self.is_asynchronous:
                target_q, state = self.engine.step(self.q, self.qd)
            else:
                # Synchronous freeze behavior: if buffer runs dry, robot freezes in place abruptly
                if self.engine.active_trajectory is not None and len(self.engine.active_trajectory) > 0:
                    target_q = self.engine.active_trajectory[0].copy()
                    self.engine.active_trajectory = self.engine.active_trajectory[1:]
                    state = ControllerState.TRACKING_NOMINAL
                else:
                    # FREEZE: holds last q without damping, causing sudden velocity stop / torque jolt
                    target_q = self.q.copy()
                    state = ControllerState.DAMPED_FALLBACK

            # 3. Simulate Robot Physics & Actuator Dynamics
            if self.env is not None:
                # Step inside Genesis World with Quadrants JIT
                rob_state = self.env.step_control(target_q, kp=self.kp, kd=self.kd)
                self.q = rob_state.joint_positions
                self.qd = rob_state.joint_velocities
                torque_norm = float(np.linalg.norm(rob_state.applied_torques))
            else:
                # High-fidelity internal dynamic model
                pos_error = target_q - self.q
                torque = self.kp * pos_error - self.kd * self.qd
                torque_norm = float(np.linalg.norm(torque))

                # Forward Euler step for joint physics: qddot = torque / inertia (unit inertia assumption)
                qddot = torque / 1.5  # Approximate link mass
                self.qd += qddot * self.dt
                self.q += self.qd * self.dt

            cycle_end_ns = time.monotonic_ns()
            cycle_time_us = int((cycle_end_ns - cycle_start_ns) / 1000)

            # 4. Telemetry logging
            if state == ControllerState.DAMPED_FALLBACK and buffer_depth == 0:
                probe.record(
                    RTMEventType.DEADLINE_MISS,
                    seq_id=self.last_seq_id,
                    buffer_depth=0,
                    joint_torque_norm=torque_norm,
                    timestamp_ns=cycle_end_ns,
                )
            else:
                probe.record(
                    RTMEventType.MOTOR_TICK,
                    seq_id=self.last_seq_id,
                    buffer_depth=buffer_depth,
                    joint_torque_norm=torque_norm,
                    timestamp_ns=cycle_end_ns,
                )

            res = ControllerTickResult(
                tick_idx=tick,
                t_now_ns=cycle_end_ns,
                target_q=target_q.copy(),
                current_q=self.q.copy(),
                current_qd=self.qd.copy(),
                state=state,
                buffer_depth=buffer_depth,
                cycle_time_us=cycle_time_us,
                torque_norm=torque_norm,
            )
            results.append(res)

            # 5. Precise Real-Time Wall-Clock Pacing (10 ms)
            next_deadline_ns += target_cycle_ns
            remaining_ns = next_deadline_ns - time.monotonic_ns()
            if remaining_ns > 50_000:  # > 50 microseconds
                time.sleep(remaining_ns / 1e9)

        probe.close()
        return results
