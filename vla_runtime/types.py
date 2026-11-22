"""
Type definitions and data structures for the Asynchronous VLA Runtime.
"""

from dataclasses import dataclass
from enum import IntEnum
import struct
import numpy as np


class ControllerState(IntEnum):
    """Execution state of the System 1 motor controller."""
    BOOTING = 0
    TRACKING_NOMINAL = 1      # Following valid VLA action chunks
    ENSEMBLING = 2            # Blending overlapping chunks
    DAMPED_FALLBACK = 3       # Buffer starved; executing inertial deceleration
    EMERGENCY_STOP = 4        # Trajectory divergence or physical collision


@dataclass(frozen=True)
class TrajectoryConfig:
    """Configuration for temporal trajectory ensembling and damping."""
    control_frequency_hz: float = 100.0   # System 1 frequency (dt = 10ms)
    chunk_horizon_steps: int = 50         # Number of future waypoints per chunk (500ms)
    joint_dof: int = 7                    # Franka Panda 7-DOF arm
    
    # Exponential blending parameter: w_i = exp(-decay_rate * i)
    decay_rate: float = 0.15
    
    # Damping parameters when buffer starves
    critical_damping_time_s: float = 0.15  # Time to bring velocity smoothly to 0 (150ms)
    min_jerk_poly_order: int = 5          # Quintic polynomial interpolation


# Binary Header Format for Shared Memory Ring Buffer:
# [magic (4B), seq_id (8B uint64), t_obs_captured_ns (8B uint64), 
#  t_produced_ns (8B uint64), chunk_length (4B uint32), dof (4B uint32), is_valid (1B bool)]
SHM_HEADER_STRUCT = struct.Struct("<4sQQQII?")
SHM_HEADER_MAGIC = b"VLA1"
SHM_HEADER_SIZE = SHM_HEADER_STRUCT.size


@dataclass
class ActionChunkPacket:
    """
    Action trajectory chunk emitted by System 2 VLA.
    actions shape: [chunk_length, joint_dof] (typically [50, 7])
    """
    seq_id: int
    t_obs_captured_ns: int   # When camera frame was sampled (monotonic ns)
    t_produced_ns: int       # When VLM finished inference and emitted chunk (monotonic ns)
    actions: np.ndarray      # Joint position targets [T, DOF] in radians
    
    @property
    def chunk_length(self) -> int:
        return self.actions.shape[0]

    @property
    def dof(self) -> int:
        return self.actions.shape[1]
