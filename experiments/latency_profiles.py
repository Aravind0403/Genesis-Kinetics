"""
Latency profile generators for realistic VLA inference and network timing simulation.
Models nominal execution, heavy-tailed compute jitter, and adversarial P99 spikes.
"""

from dataclasses import dataclass
from enum import Enum
import random
import numpy as np


class LatencyMode(Enum):
    NOMINAL = "nominal"               # Stable edge GPU inference (P50 ~80ms)
    NETWORK_JITTER = "network_jitter" # Edge-to-cloud IPC / WiFi jitter (P50 ~90ms, P95 ~140ms)
    ADVERSARIAL_SPIKE = "adversarial" # Deliberate P99 stalls (+150ms spikes to force starvation)


@dataclass
class LatencyProfile:
    mode: LatencyMode = LatencyMode.NOMINAL
    base_latency_s: float = 0.080      # 80 ms nominal
    jitter_std_s: float = 0.010        # 10 ms std dev
    spike_magnitude_s: float = 0.150   # +150 ms spike
    spike_probability: float = 0.15    # 15% chance of spike in adversarial mode

    def sample_latency_s(self, step_idx: int) -> float:
        """Samples the inference latency for the given chunk generation step."""
        if self.mode == LatencyMode.NOMINAL:
            # Normal distribution clipped at 40ms minimum
            lat = np.random.normal(self.base_latency_s, self.jitter_std_s)
            return max(0.040, float(lat))

        elif self.mode == LatencyMode.NETWORK_JITTER:
            # Log-normal distribution with long tail
            mu = np.log(self.base_latency_s)
            lat = np.random.lognormal(mu, 0.25)
            return max(0.040, float(lat))

        elif self.mode == LatencyMode.ADVERSARIAL_SPIKE:
            base = max(0.040, float(np.random.normal(self.base_latency_s, self.jitter_std_s)))
            # Trigger periodic or probabilistic spike
            if (step_idx % 6 == 0 and step_idx > 0) or (random.random() < self.spike_probability):
                return base + self.spike_magnitude_s
            return base

        return self.base_latency_s
