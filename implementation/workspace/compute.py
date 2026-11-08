"""Compute contract. No provider registration, prices, provisioning or real charges invented."""
from dataclasses import dataclass
from typing import Protocol, Callable
import os

@dataclass(frozen=True)
class Resources:
    cpu_seconds: float = 15
    wall_seconds: float = 30
    memory_bytes: int = 1073741824
    disk_bytes: int = 16777216
    gpu_count: int = 0

    def validate(self):
        if not (0 < self.cpu_seconds <= 15 and 0 < self.wall_seconds <= 30 and
                0 < self.memory_bytes <= 1073741824 and 0 < self.disk_bytes <= 16777216 and self.gpu_count == 0):
            raise ValueError('Unsupported local reference resource request')

class ComputeBackend(Protocol):
    id: str
    def estimate(self, resources: Resources) -> dict: ...
    def execute(self, argv: list[str], directory, resources: Resources, env: dict,
                disk_root, cancel: Callable[[], bool]) -> dict: ...

class LocalCompute:
    id = 'local'
    def estimate(self, resources):
        resources.validate()
        return {'backend': self.id, 'platform_charge': 0, 'electricity_cost': 'NOT_ESTIMATED',
                'supported': os.name == 'nt', 'checkpoint': 'restart_from_frozen_inputs'}

    def execute(self, argv, directory, resources, env, disk_root, cancel):
        resources.validate()
        if os.name != 'nt':
            raise RuntimeError('Windows Job Object backend required; Linux workspace sandbox not yet connected')
        from implementation.windows_process import run
        return run(argv, directory, resources.cpu_seconds, resources.wall_seconds, resources.memory_bytes,
                   env, disk_root, resources.disk_bytes, cancel=cancel)

class UnconfiguredCompute:
    def __init__(self, kind):
        if kind not in ('self_hosted', 'rlsok_cloud'):
            raise ValueError('Unknown compute kind')
        self.id = kind
    def estimate(self, resources):
        return {'backend': self.id, 'supported': False, 'estimated_cost': None, 'pricing': None,
                'billing': False, 'reason': 'No verified provider/provisioning/metering configured'}
    def execute(self, *args, **kwargs):
        raise RuntimeError('Compute backend unavailable; no allocation or charges made')
