# Genesis-Kinetics

### *Asynchronous Action-Chunk Runtime & Deterministic Flight Recorder for Genesis World*

[![Genesis-Kinetics](https://img.shields.io/badge/Genesis--Kinetics-v0.1.0-blue.svg)]()
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Genesis World](https://img.shields.io/badge/Genesis%20World-1.4.7-black.svg)](https://genesis-world.org/)
[![Quadrants JIT](https://img.shields.io/badge/Quadrants-1.4.0-orange.svg)](https://github.com/Genesis-Embodied-AI/quadrants)
[![PyTorch](https://img.shields.io/badge/PyTorch-2.x-red.svg)](https://pytorch.org/)
[![Control Frequency](https://img.shields.io/badge/System%201-100%20Hz-green.svg)]()
[![Hardware Backend](https://img.shields.io/badge/Backend-Apple%20Metal%20%2F%20CUDA-purple.svg)]()

**Genesis-Kinetics** is an open-source experimental asynchronous action-chunk runtime and deterministic flight recorder for studying compute, scheduling, and network latency disturbances in **Genesis World** (1.4.7) physics simulation.

---

## 1. Why This Runtime Exists

### The Latency Vulnerability in Hierarchical VLA Control
Frontier embodied AI architectures are increasingly adopting hierarchical control: large vision-language models planning high-level intent at 5–10 Hz, paired with agile on-device action heads executing trajectories at 100–500 Hz.

Recent open-source frameworks (such as LeRobot with Real-Time Chunking [RTC], OpenVLA, and $\pi_0$) have demonstrated the value of asynchronous inference and chunk buffering. However, robotics teams deploying hierarchical policies in dynamic simulation still face three critical systems challenges:

1. **Action Starvation Under Compute Stalls**: When an inference pass suffers an unexpected tail latency delay (due to network transfer, thermal throttling, or host scheduler contention), action chunk buffers deplete, causing robots to freeze or drop grasped payloads.
2. **Discontinuity Across Chunk Boundaries**: Stitching discrete chunk arrivals into continuous motor commands requires smooth ensembling and deterministic inertial fallback when deadlines are missed.
3. **The Causal Diagnostic Blindspot**: When a manipulation policy fails in simulation (e.g., dropping a cube), post-hoc analysis rarely reveals whether the failure stemmed from a flawed model prediction, joint velocity saturation, or a missed 10 ms control deadline caused by host OS scheduler jitter.

Genesis-Kinetics provides an empirical fault-injection testbed and microsecond-level flight recorder to systematically benchmark and diagnose these failure modes inside Genesis World simulation.

---

## 2. Implementation Status & Scope Disclosures

To ensure strict engineering transparency, the table below outlines what is currently implemented, simulated, and tested in this reference repository:

| Component | Implementation Status | Technical Disclosures & Scope |
| :--- | :--- | :--- |
| **Policy Worker (`system2_worker.py`)** | Synthetic / Parameterized Policy | Generates 50-step action chunks at 5–10 Hz with configurable latency distributions (Gaussian nominal, log-normal jitter, and injected 150 ms stalls). Enables reproducible systems benchmarking independent of specific GPU weights. |
| **Simulation Environment (`genesis_env.py`)** | Closed-Loop Genesis Simulation | Franka Emika Panda (7 arm DOFs + 2 gripper DOFs) performing tabletop manipulation in Genesis World 1.4.7 with Apple Metal acceleration (`gs.metal`). |
| **Tabletop Task (`run_tabletop_pick.py`)** | Scripted Waypoint Trajectory (IK) | 5-phase pick-and-lift task (approach $\to$ descend $\to$ grasp $\to$ 14.8 cm lift $\to$ hold) used as an integration testbed to measure tracking distortion under injected compute stalls. |
| **IPC Transport (`buffer.py`)** | POSIX Shared Memory (`multiprocessing.shared_memory`) | Double-buffered SPSC ring buffer with monotonic sequence counters. Non-blocking hot path (no OS mutexes or semaphores acquired in the 100 Hz control loop). |
| **Tensor Interoperability (`benchmark_dlpack_zerocopy.py`)** | In-Process DLPack Memory Sharing | Direct zero-copy pointer sharing between Genesis C++/Metal memory structures and PyTorch tensors, eliminating intermediate NumPy heap allocations. |
| **Telemetry & Flight Recorder (`telemetry/`)** | Non-blocking UDS Binary Recorder | Microsecond `.tick` binary records streaming to disk via `/tmp/rtm.sock`, capturing the full causal timeline from camera ingest to motor torque impulse. |
| **Hardware Backends** | Tested on Apple Silicon Metal | Verified on macOS Apple Silicon Metal (`gs.metal`); architectural support for Linux CUDA (`gs.cuda`). |

---

## 3. System Architecture & Concurrency Model

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           System 2: Disaggregated VLA                       │
│                (Separate Process, 5 - 10 Hz, GPU Worker)                    │
│                                                                             │
│  Camera Frame [t_obs] ──► Chunked Vision Prefill ──► Zero-Copy KV Transfer  │
│                                                            │                │
│                                                            ▼                │
│                             Agile Action Head ──► Action Chunk (T = 50)     │
└───────────────────────────────────────────────┬─────────────────────────────┘
                                                │ Non-Blocking IPC (Double-Buffered SPSC POSIX shm)
                                                │ Packet: {t_obs, seq_id, chunk[50, 7]}
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           System 1: Motor Controller                        │
│                (Separate Real-Time Process, 100 Hz, Genesis)                │
│                                                                             │
│  ┌───────────────────────┐   ┌────────────────────────┐                     │
│  │ Chunk Time-Alignment  │──►│ Exponential Ensembler  │                     │
│  │  - Drop elapsed steps │   │  - Blend overlapping   │                     │
│  │  - Check buffer depth │   │    trajectories        │                     │
│  └───────────┬───────────┘   └───────────┬────────────┘                     │
│              │ (Buffer empty?)           │ (Buffer valid)                   │
│              ▼                           ▼                                  │
│  ┌───────────────────────┐   ┌────────────────────────┐                     │
│  │ Damped Inertial Hold  │   │ Zero-Copy DLPack       │                     │
│  │ (Quintic Damping +    │   │ arr.to_torch(copy=False│                     │
│  │  Gravity Comp)        │   │ Native Memory Sharing) │                     │
│  └───────────┬───────────┘   └───────────┬────────────┘                     │
│              │                           │                                  │
│              └───────────────┬───────────┘                                  │
│                              ▼                                              │
│               Genesis World Physics Step (100 Hz)                           │
│               Franka Emika Panda (7 Arm DOFs + 2 Gripper DOFs)              │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │ Non-blocking Telemetry (UDS Socket: /tmp/rtm.sock)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                 Observability Layer (real-time-monitor)                     │
│                                                                             │
│  UDS `/tmp/rtm.sock` ──► Binary `.tick` Stream ──► Interactive Web Dashboard│
│  Events: [CAMERA_INGEST, VLM_PREFILL, CHUNK_GEN, IPC_HANDOVER, MOTOR_TICK] │
└─────────────────────────────────────────────────────────────────────────────┘
```

> [!NOTE]
> **IPC Concurrency Model & Guarantees**:
> The inter-process communication layer between System 2 (5–10 Hz) and System 1 (100 Hz) uses a double-buffered Single-Producer Single-Consumer (SPSC) layout backed by POSIX shared memory (`multiprocessing.shared_memory`).
> - **Zero OS Mutex Contention**: Neither process acquires OS mutexes, semaphores, futexes, or file locks in the control loop. System 1 (100 Hz) is decoupled and never blocks waiting on a lock held by System 2.
> - **Slot Ownership & Snapshot Copying**: The producer writes new chunk payloads into the inactive slot before updating the monotonic `write_cursor`. The consumer inspects the cursor, copies a local snapshot of the active slot into process-local memory, and verifies sequence continuity.
> - **Python Interpreter vs. Native Guarantees**: In this pure-Python reference implementation, synchronization relies on atomic integer cursor assignments within the CPython runtime. For strict C++11 memory-model guarantees (`memory_order_release` / `memory_order_acquire` hardware memory fences across arbitrary CPU cores on multi-socket NUMA systems), a compiled C++/Rust extension is standard in production. Our pure-Python implementation provides an accessible, non-blocking baseline without requiring external C toolchain compilation.

---

## 4. Empirical Benchmarks & Quantitative Findings

### Benchmark A: Stress-Test Under Injected Latency Stalls (+150 ms)
To evaluate controller robustness against tail-latency delays, we benchmark our asynchronous runtime against a standard synchronous chunk-execution baseline (where the controller halts and waits synchronously when the policy buffer empties). Both baselines operate with identical PD controller gains, gravity compensation, and Genesis physics parameters under an injected +150 ms compute stall workload:

```
================================================================================
 📊 COUNTERFACTUAL STRESS-TEST BENCHMARK RESULTS
================================================================================
Configuration                       | Mean Jerk (rad/s³) | Peak Torque (N·m) | Miss / Degraded Rate (%)
--------------------------------------------------------------------------------
Sync Baseline (Nominal)             |              63.79 |             29.80 |           4.4% (Miss)
Sync Baseline (+150ms Spikes)       |             111.85 |             33.35 |          10.0% (Miss)
Our Async Runtime (Nominal)         |              66.90 |             52.44 |           4.0% (Degraded)
Our Async Runtime (+150ms Spikes)   |              82.38 |             55.54 |           5.2% (Degraded)
================================================================================
```

#### Analysis & Control Trade-Offs:
* **26.3% Reduction in Mechanical Jerk Under Stalls**: When an injected +150 ms stall occurs, the synchronous baseline freezes in place, causing violent joint velocity discontinuities ($111.85\text{ rad/s}^3$) upon resuming. Our asynchronous runtime smoothly blends overlapping chunk horizons and transitions into quintic velocity deceleration ($82.38\text{ rad/s}^3$).
* **Metric Definitions**:
  * *Sync Miss Rate (10.0%)*: The percentage of total control cycles where the robot was completely frozen waiting for delayed inference.
  * *Async Degraded Rate (5.2%)*: The percentage of control cycles where the action buffer was starved and the controller operated in inertial quintic damping mode.
* **Actuator Torque Dynamics & Damped Continuation**:
  Under injected stalls, the asynchronous runtime commands higher peak torque ($55.54\text{ N}\cdot\text{m}$) than the synchronous baseline ($33.35\text{ N}\cdot\text{m}$). This is an intentional control engineering trade-off:
  - The **Synchronous Baseline** exhibits lower instantaneous commanded torque during starvation because it abruptly halts target position updates. However, this introduces severe kinematic shock when delayed chunks arrive ($111.85\text{ rad/s}^3$ jerk) and causes payload slippage under gravity.
  - **Genesis-Kinetics** commits dynamic braking torque via quintic polynomial deceleration while actively sustaining joint gravity compensation. Committing controlled actuator torque to preserve mechanical momentum continuity prevents payload loss. All measured torques remain comfortably within the Franka Emika Panda rated joint torque limits (Joints 1–4: 87 N·m, Joints 5–7: 12 N·m).

---

### Benchmark B: In-Process Zero-Copy DLPack vs. Copy-Based Tensor Handover (25,000 Cycles)
Benchmarking in-process memory pointer sharing between Genesis World physics structures and PyTorch tensors:

```
====================================================================================
 📊 MICROBENCHMARK RESULTS: TENSOR HANDOVER PERFORMANCE
====================================================================================
Pattern                          | Latency per Tick   | Bytes Copied   | Effective Throughput
------------------------------------------------------------------------------------
Copy-Based (Standard Tensor Copy)|         3.368 µs     |      33.38 MB   |      396.4 MB/s
Zero-Copy DLPack Streaming       |         2.628 µs     |       0.00 MB   |      508.0 MB/s
====================================================================================
```

* **22.0% Overhead Reduction**: Direct memory pointer projection via DLPack avoids intermediate memory copying ($3.368\,\mu\text{s} \to 2.628\,\mu\text{s}$, a saving of $0.740\,\mu\text{s}$ per tick).
* **Heap Allocation Avoidance**: Eliminates **33.4 MB** of temporary Python heap allocations over 25,000 cycles, helping mitigate Python garbage collection pauses during high-frequency control loops.
* **Budget Context**: At 100 Hz ($10,000\,\mu\text{s}$ budget), tensor handover consumes less than **0.03% of the control loop budget**.

---

### Benchmark C: Genesis World 3D Tabletop Rollout
* **Simulation Setup**: Franka Emika Panda tabletop pick-and-lift task executed in Genesis World 1.4.7 natively accelerated by Apple Metal (`gs.metal`).
* **5-Phase Trajectory**: Pre-grasp approach $\to$ descend $\to$ grasp $\to$ 14.8 cm vertical lift $\to$ hold.
* **Control Performance**: 250 control ticks executed at simulated 100 Hz (10 ms physics step) with 0 deadline misses, recorded to `tabletop_cube_pick.mp4` (1.1 MB, 720x540, 50 FPS).

---

## 5. Closed-Loop Flight Recorder & Causal Diagnostics

The core contribution of Genesis-Kinetics is the **Closed-Loop Flight Recorder**—a microsecond-precision telemetry subsystem designed to resolve the diagnostic blindspot in embodied AI simulation.

### Telemetry Packet Provenance Schema
Every control cycle emits a structured binary `.tick` packet capturing complete end-to-end timing provenance:

| Field | Type | Description |
| :--- | :--- | :--- |
| `tick_id` | `uint64` | Monotonic control loop step counter |
| `t_obs` | `float64` | Observation capture timestamp (monotonic microseconds) |
| `t_prefill_end` | `float64` | Vision prefill encoder completion timestamp |
| `t_publish` | `float64` | Action chunk IPC publication timestamp |
| `t_motor` | `float64` | Motor controller deadline execution timestamp |
| `buffer_horizon` | `int32` | Number of unexecuted action steps remaining in the queue |
| `controller_state`| `enum` | Controller mode (`NOMINAL`, `BLENDING`, `DAMPED_HOLD`, `BUFFER_EMPTY`) |
| `torque_norm` | `float32` | Instantaneous L2 norm of commanded joint torques (N·m) |
| `jerk` | `float32` | Instantaneous mechanical jerk (rad/s³) |

### Isolating Causal Root Causes
When a robot fails to grasp or lift an object in simulation, the flight recorder allows engineers to definitively separate algorithmic policy errors from systems and scheduling latency:

```
[SCENARIO: ROBOT DROPS PAYLOAD AT t = 1.84s]
├── Without Flight Recorder:
│   └── Engineer assumes policy network output an incorrect joint position.
│       Action: Weeks spent fine-tuning VLA weights or collecting new demonstrations.
│
└── With Genesis-Kinetics Flight Recorder:
    ├── Step 142 (t = 1.42s): System 2 begins VLM inference pass.
    ├── Step 157 (t = 1.57s): Injected stall delays chunk arrival by +150 ms.
    ├── Step 168 (t = 1.68s): Buffer horizon drops to 0. Controller enters DAMPED_HOLD.
    └── Step 184 (t = 1.84s): Cube slips due to prolonged buffer starvation during lift.
    └── Causal Conclusion: Systems latency failure (tail compute delay), NOT a policy defect.
```

---

## 6. Interactive Flight Inspector & Web Dashboard

### 1. Interactive Terminal Rewinder
Run the interactive scrubber to step through rollout history, inspect latency milestones, and view causal state transitions:
```bash
python3 experiments/counterfactual_inspector.py --auto
```

### 2. Standalone HTML Flight Dashboard
Open the interactive flight recorder dashboard in any modern web browser:
```bash
open telemetry/dashboard.html
```

Features:
* **Embedded Video & Live Scrubber Sync**: Video playback and telemetry scrubbing are tied to a single unified `requestAnimationFrame` loop.
* **Side-by-Side Synchronized Charts**: Real-time waveform unrolling for Action Buffer Horizon and Joint Torque Norm with glowing oscilloscope tracking heads.
* **Diagnostic Milestone Cards**: Instant navigation to key simulation events (e.g., approach, grasp, stall injection, recovery).

---

## 7. Repository Structure

```
Genesis_AI/
├── vla_runtime/
│   ├── types.py                     # Data contracts, ActionChunkPacket, ControllerState
│   ├── buffer.py                    # Non-blocking double-buffered SPSC ring buffer (POSIX shm)
│   ├── trajectory.py                # Elapsed-step truncation, exponential ensembling & quintic dampener
│   ├── disaggregated_serving.py     # Chunked vision prefill + zero-copy KV transfer + action head
│   ├── system1_controller.py        # 100 Hz real-time controller loop paced by monotonic clock
│   ├── system2_worker.py            # Isolated-process perception server with latency profiles
│   └── genesis_env.py               # Genesis World 1.4.7 + Franka Panda tabletop simulation
├── telemetry/
│   ├── rtm_probe.py                 # Non-blocking UDS client transmitting binary .tick records
│   ├── recorder.py                  # Flight recorder daemon streaming events to disk
│   ├── visualizer.py                # Latency percentiles & deadline miss reporter
│   ├── generate_dashboard.py        # Generates standalone interactive dashboard.html
│   └── dashboard.html               # Interactive web flight recorder dashboard
├── experiments/
│   ├── latency_profiles.py          # Nominal, network jitter, and adversarial stall distributions
│   ├── counterfactual_benchmark.py  # Automated Sync vs. Async 4-way stress test
│   ├── counterfactual_inspector.py  # Interactive terminal timeline scrubber & causal diagnoser
│   ├── benchmark_dlpack_zerocopy.py # Zero-copy DLPack vs copy-based IPC microbenchmark
│   ├── run_disaggregated_vla.py     # End-to-end disaggregated serving pipeline demonstration
│   └── run_tabletop_pick.py         # 3D closed-loop tabletop cube-picking simulation rollout
├── tests/
│   ├── test_vla_core.py             # Unit tests for ring buffer, ensembling, damping & Genesis
│   ├── test_telemetry.py            # Socket streaming & .tick binary parsing tests
│   └── test_disaggregated_serving.py # Chunked prefill & action head unit tests
├── tabletop_cube_pick.mp4            # Rendered 3D visual rollout video (1.1 MB)
├── README.md                        # Rigorous systems documentation & disclosures
└── .gitignore                       # Clean Git tracking configuration
```

---

## 8. Quickstart Guide

### Prerequisites
* Python 3.10+
* PyTorch 2.x
* Genesis World (`pip install genesis-world`)
* macOS (Metal) or Linux (CUDA)

### 1. Run Unit Tests
```bash
python3 -m pytest tests/ -v
# 7 passed in 12.5s
```

### 2. Run Stress-Test Benchmark
```bash
python3 experiments/counterfactual_benchmark.py
```

### 3. Run In-Process DLPack Microbenchmark
```bash
python3 experiments/benchmark_dlpack_zerocopy.py
```

### 4. Run Disaggregated VLA Serving Demonstration
```bash
python3 experiments/run_disaggregated_vla.py
```

### 5. Run Live Genesis World Tabletop Cube-Picking Rollout
```bash
python3 experiments/run_tabletop_pick.py
```

### 6. Launch Interactive Flight Rewinder
```bash
python3 experiments/counterfactual_inspector.py --auto
```
