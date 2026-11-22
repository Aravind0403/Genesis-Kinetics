"""
Temporal Trajectory Blending and Damped Continuation Engine.

Responsibilities:
1. Dynamic Chunk Truncation: Drop elapsed steps based on (t_now - t_obs_captured).
2. Exponential Recency Ensembling: Smoothly blend newly arrived action chunks with
   the currently executing trajectory window.
3. Damped Continuation (Fallback): When latency causes buffer starvation, execute
   inertial velocity damping (minimum jerk quintic polynomial) to bring joints to rest
   safely without mechanical shock or sudden torque collapse.
"""

import math
import numpy as np
from typing import Optional, Tuple
from vla_runtime.types import ActionChunkPacket, ControllerState, TrajectoryConfig


class TrajectoryEngine:
    """
    Real-time trajectory manager running inside System 1 at 100 Hz.
    """

    def __init__(self, config: Optional[TrajectoryConfig] = None):
        self.config = config or TrajectoryConfig()
        self.dt = 1.0 / self.config.control_frequency_hz  # 0.01s (10ms)
        
        # State tracking
        self.active_trajectory: Optional[np.ndarray] = None  # Shape: [T_remaining, DOF]
        self.current_step_idx: int = 0
        self.state: ControllerState = ControllerState.BOOTING
        
        # Fallback damping trajectory buffer
        self.fallback_steps: Optional[np.ndarray] = None
        self.fallback_idx: int = 0

    def ingest_chunk(self, chunk: ActionChunkPacket, t_now_ns: int):
        """
        Ingest a newly arrived action chunk from System 2.
        Calculates how many steps elapsed during perception inference and blends.
        """
        elapsed_s = max(0.0, (t_now_ns - chunk.t_obs_captured_ns) / 1e9)
        elapsed_steps = int(math.floor(elapsed_s / self.dt))

        if elapsed_steps >= chunk.chunk_length:
            # Chunk is completely obsolete (took longer than entire horizon)
            return

        # Slice off already-elapsed actions
        valid_chunk = chunk.actions[elapsed_steps:].copy()
        valid_len = valid_chunk.shape[0]

        if self.active_trajectory is None or len(self.active_trajectory) == 0:
            # Cold start: adopt chunk directly
            self.active_trajectory = valid_chunk
            self.state = ControllerState.TRACKING_NOMINAL
            self.fallback_steps = None
            return

        # Temporal Ensembling: Blend overlapping segment between active and new
        overlap_len = min(len(self.active_trajectory), valid_len)
        if overlap_len > 0:
            # Exponential weights prioritizing newer chunk while retaining smooth transition
            # w_new increases from 0 -> 1 over the overlap window
            indices = np.arange(overlap_len)
            w_new = 1.0 - np.exp(-self.config.decay_rate * (indices + 1))
            w_new = w_new[:, np.newaxis]  # Broadcast across DOF
            
            # Blend overlapping slice
            blended_overlap = (1.0 - w_new) * self.active_trajectory[:overlap_len] + w_new * valid_chunk[:overlap_len]
            
            # Suffix: append any tail from the new chunk beyond the current active trajectory
            if valid_len > overlap_len:
                new_tail = valid_chunk[overlap_len:]
                self.active_trajectory = np.vstack([blended_overlap, new_tail])
            else:
                self.active_trajectory = blended_overlap
                
            self.state = ControllerState.ENSEMBLING
        else:
            self.active_trajectory = valid_chunk
            self.state = ControllerState.TRACKING_NOMINAL

        # Clear any fallback state since we have fresh commands
        self.fallback_steps = None

    def plan_damped_stop(self, current_q: np.ndarray, current_qd: np.ndarray):
        """
        Triggered when action buffer starves (P99 latency spike).
        Generates a quintic minimum-jerk trajectory bringing joint velocities
        from current_qd to 0 over `critical_damping_time_s`.
        
        Quintic polynomial: s(t) = 10*(t/T)^3 - 15*(t/T)^4 + 6*(t/T)^5
        Position trajectory: q(t) = q0 + qd0 * t * (1 - s(t)) + (deceleration profile)
        """
        T = self.config.critical_damping_time_s
        n_steps = max(2, int(T / self.dt))
        t_arr = np.linspace(0, T, n_steps)
        tau = t_arr / T  # Normalized time in [0, 1]

        # Quintic polynomial zero-acceleration / zero-velocity end conditions
        # Velocity profile: qd(t) = qd0 * (1 - 10*tau^3 + 15*tau^4 - 6*tau^5)
        # Position profile: integrate velocity profile
        poly_pos = tau - (10.0 / 4.0) * (tau ** 4) + (15.0 / 5.0) * (tau ** 5) - (6.0 / 6.0) * (tau ** 6)
        
        # Waypoints: q(t) = current_q + current_qd * T * poly_pos
        self.fallback_steps = current_q[np.newaxis, :] + np.outer(poly_pos * T, current_qd)
        self.fallback_idx = 0
        self.state = ControllerState.DAMPED_FALLBACK

    def step(self, current_q: np.ndarray, current_qd: np.ndarray) -> Tuple[np.ndarray, ControllerState]:
        """
        Called every 10 ms (100 Hz) by System 1.
        Returns:
          target_q: Joint position target for this tick [7]
          state: Current controller state
        """
        # Case 1: Active trajectory has steps available
        if self.active_trajectory is not None and len(self.active_trajectory) > 0:
            target_q = self.active_trajectory[0].copy()
            self.active_trajectory = self.active_trajectory[1:]
            return target_q, self.state

        # Case 2: Buffer starved! Enter or continue Damped Fallback
        if self.fallback_steps is None:
            self.plan_damped_stop(current_q, current_qd)

        if self.fallback_idx < len(self.fallback_steps):
            target_q = self.fallback_steps[self.fallback_idx].copy()
            self.fallback_idx += 1
            return target_q, ControllerState.DAMPED_FALLBACK

        # Case 3: Fully stopped at target, holding position
        return current_q.copy(), ControllerState.DAMPED_FALLBACK
