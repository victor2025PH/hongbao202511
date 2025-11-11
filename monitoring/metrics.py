# -*- coding: utf-8 -*-
"""
Minimal Prometheus-style metrics helpers.

Why not use ``prometheus_client``?
- 减少额外依赖；在资源受限或离线环境也能工作。
- 仅实现项目当前需要的 Counter / Histogram，够用且便于拓展。

Usage::

    from monitoring.metrics import counter, histogram

    REQUEST_COUNTER = counter(
        "app_request_total",
        "Total requests handled.",
        label_names=("endpoint", "status"),
    )

    LATENCY_HIST = histogram(
        "app_request_seconds",
        "Request latency in seconds.",
        label_names=("endpoint", "status"),
    )

    def handle(endpoint):
        start = time.perf_counter()
        try:
            ...
            REQUEST_COUNTER.inc(endpoint=endpoint, status="success")
            LATENCY_HIST.observe(time.perf_counter() - start, endpoint=endpoint, status="success")
        except Exception:
            REQUEST_COUNTER.inc(endpoint=endpoint, status="error")
            LATENCY_HIST.observe(time.perf_counter() - start, endpoint=endpoint, status="error")
            raise

``render_prometheus()`` 会在 `/metrics` 中调用，生成文本格式输出。
"""

from __future__ import annotations

import math
import threading
from bisect import bisect_right
from contextlib import contextmanager
from time import perf_counter
from typing import Dict, Iterable, List, Sequence, Tuple


LabelTuple = Tuple[str, ...]


class _BaseMetric:
    __slots__ = ("name", "description", "label_names", "_lock")

    def __init__(self, name: str, description: str, label_names: Sequence[str] | None = None) -> None:
        self.name = name
        self.description = description or ""
        self.label_names: Tuple[str, ...] = tuple(label_names or ())
        self._lock = threading.Lock()

    # ---- 工具 ----
    def _normalize_labels(self, labels: Dict[str, str]) -> LabelTuple:
        if not self.label_names:
            return tuple()
        return tuple(str(labels.get(key, "")) for key in self.label_names)

    def _format_labels(self, values: LabelTuple) -> str:
        if not self.label_names:
            return ""
        pairs = []
        for key, value in zip(self.label_names, values):
            escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
            pairs.append(f'{key}="{escaped}"')
        return "{" + ",".join(pairs) + "}"

    def _format_labels_with_extra(self, values: LabelTuple, extra: Tuple[str, str]) -> str:
        if not self.label_names and not extra:
            return ""
        pairs = []
        for key, value in zip(self.label_names, values):
            escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
            pairs.append(f'{key}="{escaped}"')
        if extra:
            k, v = extra
            escaped = v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
            pairs.append(f'{k}="{escaped}"')
        return "{" + ",".join(pairs) + "}"


class CounterMetric(_BaseMetric):
    __slots__ = ("_samples",)

    def __init__(self, name: str, description: str, label_names: Sequence[str] | None = None) -> None:
        super().__init__(name, description, label_names)
        self._samples: Dict[LabelTuple, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("Counter cannot be decreased.")
        key = self._normalize_labels(labels)
        with self._lock:
            self._samples[key] = self._samples.get(key, 0.0) + float(amount)

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.description}",
            f"# TYPE {self.name} counter",
        ]
        for key, value in sorted(self._samples.items()):
            lines.append(f"{self.name}{self._format_labels(key)} {value}")
        return lines


class HistogramMetric(_BaseMetric):
    __slots__ = ("buckets", "_samples")

    def __init__(
        self,
        name: str,
        description: str,
        label_names: Sequence[str] | None = None,
        buckets: Iterable[float] | None = None,
    ) -> None:
        super().__init__(name, description, label_names)
        default = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10)
        self.buckets: Tuple[float, ...] = tuple(sorted(buckets)) if buckets else default
        self._samples: Dict[LabelTuple, Dict[str, object]] = {}

    def observe(self, value: float, **labels: str) -> None:
        if math.isnan(value):
            return
        key = self._normalize_labels(labels)
        with self._lock:
            state = self._samples.setdefault(
                key,
                {
                    "bucket_counts": [0 for _ in range(len(self.buckets) + 1)],  # 最后一格为 +Inf
                    "sum": 0.0,
                    "count": 0,
                },
            )
            bucket_counts: List[int] = state["bucket_counts"]  # type: ignore[assignment]
            index = bisect_right(self.buckets, value)
            bucket_counts[index] += 1
            state["sum"] = float(state["sum"]) + float(value)
            state["count"] = int(state["count"]) + 1

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.description}",
            f"# TYPE {self.name} histogram",
        ]
        for key, state in sorted(self._samples.items()):
            bucket_counts: List[int] = state["bucket_counts"]  # type: ignore[assignment]
            cumulative = 0
            for idx, bound in enumerate(self.buckets):
                cumulative += bucket_counts[idx]
                labels = self._format_labels_with_extra(key, ("le", str(bound)))
                lines.append(f"{self.name}_bucket{labels} {cumulative}")
            # +Inf bucket
            cumulative += bucket_counts[-1]
            labels_inf = self._format_labels_with_extra(key, ("le", "+Inf"))
            lines.append(f"{self.name}_bucket{labels_inf} {cumulative}")

            base_labels = self._format_labels(key)
            lines.append(f"{self.name}_count{base_labels} {state['count']}")
            lines.append(f"{self.name}_sum{base_labels} {state['sum']}")
        return lines


class MetricRegistry:
    """
    负责存储与输出所有指标，确保重名指标共享实例。
    """

    def __init__(self) -> None:
        self._counters: Dict[str, CounterMetric] = {}
        self._histograms: Dict[str, HistogramMetric] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, description: str, label_names: Sequence[str] | None = None) -> CounterMetric:
        with self._lock:
            existing = self._counters.get(name)
            if existing:
                if existing.label_names != tuple(label_names or ()):
                    raise ValueError(f"Counter {name} already registered with labels {existing.label_names}")
                return existing
            counter_metric = CounterMetric(name, description, label_names)
            self._counters[name] = counter_metric
            return counter_metric

    def histogram(
        self,
        name: str,
        description: str,
        label_names: Sequence[str] | None = None,
        buckets: Iterable[float] | None = None,
    ) -> HistogramMetric:
        with self._lock:
            existing = self._histograms.get(name)
            if existing:
                if existing.label_names != tuple(label_names or ()):
                    raise ValueError(f"Histogram {name} already registered with labels {existing.label_names}")
                return existing
            hist_metric = HistogramMetric(name, description, label_names, buckets=buckets)
            self._histograms[name] = hist_metric
            return hist_metric

    def render_prometheus(self) -> List[str]:
        lines: List[str] = []
        for metric in sorted(self._counters.values(), key=lambda m: m.name):
            lines.extend(metric.render())
        for metric in sorted(self._histograms.values(), key=lambda m: m.name):
            lines.extend(metric.render())
        return lines


# ---- 模块级注册表 ----
_REGISTRY = MetricRegistry()


def counter(name: str, description: str, label_names: Sequence[str] | None = None) -> CounterMetric:
    return _REGISTRY.counter(name, description, label_names)


def histogram(
    name: str,
    description: str,
    label_names: Sequence[str] | None = None,
    buckets: Iterable[float] | None = None,
) -> HistogramMetric:
    return _REGISTRY.histogram(name, description, label_names, buckets=buckets)


def render_prometheus() -> List[str]:
    """
    返回 Prometheus 文本格式的多行字符串列表（不带结尾换行）。
    """
    return _REGISTRY.render_prometheus()


@contextmanager
def record_latency(metric: HistogramMetric, **labels: str):
    """
    A small helper for timing code blocks::

        with record_latency(LATENCY_METRIC, operation="create"):
            do_something()
    """
    start = perf_counter()
    try:
        yield
    finally:
        metric.observe(perf_counter() - start, **labels)


__all__ = [
    "counter",
    "histogram",
    "record_latency",
    "render_prometheus",
]


