"""
Lock-free, double-buffered Single-Producer Single-Consumer (SPSC) shared memory
ring buffer for zero-copy ActionChunk handover between System 2 and System 1.
"""

import mmap
from multiprocessing import shared_memory
import struct
import time
from typing import Optional, Tuple
import numpy as np

from vla_runtime.types import (
    ActionChunkPacket,
    SHM_HEADER_STRUCT,
    SHM_HEADER_MAGIC,
    SHM_HEADER_SIZE,
)


class SharedActionRingBuffer:
    """
    Lock-free Double-Buffered SPSC Ring Buffer in POSIX Shared Memory.
    
    Layout:
      Slot 0: [Header (37B) + Actions Data (T * DOF * 8B float64)]
      Slot 1: [Header (37B) + Actions Data (T * DOF * 8B float64)]
      Control Area:
        - write_cursor: uint64 (atomic index updated by System 2 Producer)
        - read_cursor: uint64 (atomic index read by System 1 Consumer)
        - shutdown_flag: uint32
    """
    CONTROL_STRUCT = struct.Struct("<QQI")  # write_cursor, read_cursor, shutdown_flag
    CONTROL_SIZE = CONTROL_STRUCT.size

    def __init__(
        self,
        name: str = "vla_action_ring_buffer",
        chunk_horizon: int = 50,
        joint_dof: int = 7,
        create: bool = False,
    ):
        self.name = name
        self.chunk_horizon = chunk_horizon
        self.joint_dof = joint_dof
        self.slot_payload_size = chunk_horizon * joint_dof * 8  # float64
        self.slot_total_size = SHM_HEADER_SIZE + self.slot_payload_size
        self.num_slots = 2  # Double-buffering
        self.total_size = self.CONTROL_SIZE + (self.num_slots * self.slot_total_size)
        
        self.create = create
        if create:
            # Clean up existing if present
            try:
                existing = shared_memory.SharedMemory(name=self.name)
                existing.close()
                existing.unlink()
            except FileNotFoundError:
                pass
            self.shm = shared_memory.SharedMemory(name=self.name, create=True, size=self.total_size)
            # Initialize control area
            self._write_control(write_cursor=0, read_cursor=0, shutdown=0)
        else:
            self.shm = shared_memory.SharedMemory(name=self.name, create=False)

    def _get_slot_offset(self, slot_idx: int) -> int:
        return self.CONTROL_SIZE + ((slot_idx % self.num_slots) * self.slot_total_size)

    def _read_control(self) -> Tuple[int, int, int]:
        data = self.shm.buf[:self.CONTROL_SIZE]
        return self.CONTROL_STRUCT.unpack(data)

    def _write_control(self, write_cursor: int, read_cursor: int, shutdown: int):
        packed = self.CONTROL_STRUCT.pack(write_cursor, read_cursor, shutdown)
        self.shm.buf[:self.CONTROL_SIZE] = packed

    def set_shutdown(self):
        """Signals shutdown to all attached processes."""
        w, r, _ = self._read_control()
        self._write_control(w, r, shutdown=1)

    def is_shutdown(self) -> bool:
        _, _, s = self._read_control()
        return s == 1

    def produce(self, packet: ActionChunkPacket) -> bool:
        """
        System 2 (Producer): Write a new action chunk into the alternate slot.
        Lock-free: writes payload first, then updates write_cursor atomically.
        """
        w_cur, r_cur, shutdown = self._read_control()
        if shutdown:
            return False

        target_slot = (w_cur + 1) % self.num_slots
        offset = self._get_slot_offset(target_slot)

        # 1. Pack Header
        actions_data = packet.actions.astype(np.float64, copy=False)
        header_bytes = SHM_HEADER_STRUCT.pack(
            SHM_HEADER_MAGIC,
            packet.seq_id,
            packet.t_obs_captured_ns,
            packet.t_produced_ns,
            packet.chunk_length,
            packet.dof,
            True,  # is_valid
        )

        # 2. Write Header & Payload
        payload_bytes = actions_data.tobytes()
        full_block = header_bytes + payload_bytes
        self.shm.buf[offset: offset + len(full_block)] = full_block

        # 3. Memory fence / Atomic commit: increment write_cursor
        self._write_control(w_cur + 1, r_cur, shutdown)
        return True

    def consume_latest(self, last_seen_seq: int = -1) -> Optional[ActionChunkPacket]:
        """
        System 1 (Consumer): Read latest chunk if a newer one was produced.
        Returns None if no newer chunk is available.
        """
        w_cur, _, shutdown = self._read_control()
        if w_cur == 0:
            return None  # Nothing written yet

        active_slot = w_cur % self.num_slots
        offset = self._get_slot_offset(active_slot)

        # Read header
        header_buf = self.shm.buf[offset: offset + SHM_HEADER_SIZE]
        (
            magic,
            seq_id,
            t_obs_ns,
            t_prod_ns,
            chunk_len,
            dof,
            is_valid,
        ) = SHM_HEADER_STRUCT.unpack(header_buf)

        if magic != SHM_HEADER_MAGIC or not is_valid or seq_id <= last_seen_seq:
            return None

        # Read payload without copy using numpy frombuffer
        data_start = offset + SHM_HEADER_SIZE
        data_end = data_start + (chunk_len * dof * 8)
        actions = np.frombuffer(self.shm.buf[data_start:data_end], dtype=np.float64).reshape((chunk_len, dof)).copy()

        # Update read_cursor
        _, r_cur, _ = self._read_control()
        self._write_control(w_cur, r_cur + 1, shutdown)

        return ActionChunkPacket(
            seq_id=seq_id,
            t_obs_captured_ns=t_obs_ns,
            t_produced_ns=t_prod_ns,
            actions=actions,
        )

    def close(self):
        self.shm.close()

    def unlink(self):
        try:
            self.shm.unlink()
        except FileNotFoundError:
            pass
