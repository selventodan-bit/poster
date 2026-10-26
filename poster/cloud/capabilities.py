"""Measured local capacity, explicit provider policy and a separate runtime lifecycle."""

from dataclasses import asdict, dataclass
from enum import StrEnum
import os
import shutil
import sys

import psutil

from .errors import CloudError


class Lifecycle(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    READY = "READY"
    STARTING = "STARTING"
    AVAILABLE = "AVAILABLE"
    EXECUTING = "EXECUTING"
    FINISHED = "FINISHED"
    STOPPING = "STOPPING"
    EXPIRED = "EXPIRED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Capacity:
    provider: str
    cpu: int
    memory_mb: int
    disk_mb: int
    browser: bool
    max_parallel: int
    policy: str
    automatic: bool = False

    def public(self):
        return asdict(self)

    def validate(self, bundle, count, parallel):
        if (
            type(count) is not int
            or not 1 <= count <= 1000
            or type(parallel) is not int
            or not 1 <= parallel <= self.max_parallel
        ):
            raise CloudError("CAPABILITY")
        if bundle.manifest["capabilities"]["browser"] and not self.browser:
            raise CloudError("BROWSER_UNAVAILABLE")
        if sys.version_info < (3, 11) or self.disk_mb < 256 or self.memory_mb < 256:
            raise CloudError("RUNTIME_UNAVAILABLE")


def probe(provider="generic", *, root=None):
    if provider not in {"generic", "colab", "molab"}:
        raise CloudError("CAPABILITY")
    from poster.browser import find_browser

    try:
        browser = bool(find_browser())
    except (ValueError, OSError):
        browser = False
    cpu = os.cpu_count() or 1
    memory = psutil.virtual_memory().available // (1024 * 1024)
    disk = shutil.disk_usage(root or os.getcwd()).free // (1024 * 1024)
    # HTTP/data work is chiefly I/O bound. Browser runs have a higher RAM cost.
    maximum = max(1, min(32, cpu * 4, memory // (512 if browser else 256)))
    policy = "measured local resources; queue limits still apply"
    if provider in {"colab", "molab"}:
        maximum = 1
        policy = "one interactive notebook run; additional provider entitlement/policy not verified"
    return Capacity(provider, cpu, memory, disk, browser, maximum, policy)
