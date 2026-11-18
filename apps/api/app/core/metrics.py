"""In-process operational metrics, exposed in the Prometheus text format at `/metrics`.

Each API process keeps its own counters; a scraper collects every replica and sums them.
Labels only ever hold bounded, non-sensitive values: route templates (never concrete paths or
IDs), status codes, error codes, model names, operation kinds and outcomes. Never a user,
workspace, document, question, key or URL.
"""

import math
import re
import threading
from collections import defaultdict
from dataclasses import dataclass

# Seconds. Covers fast API requests up to long streamed answers.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:/{}\-]")


@dataclass(frozen=True)
class Metric:
    name: str
    kind: str  # counter | histogram
    help: str


METRICS = {
    m.name: m
    for m in (
        Metric("http_requests_total", "counter", "HTTP requests by method, route and status"),
        Metric("http_request_duration_seconds", "histogram", "HTTP request duration by route"),
        Metric("app_errors_total", "counter", "Error responses by error code and status"),
        Metric("llm_requests_total", "counter", "Chat model calls by model, mode and outcome"),
        Metric("llm_duration_seconds", "histogram", "Chat model call duration"),
        Metric("llm_first_token_seconds", "histogram", "Time to the first streamed output"),
        Metric("llm_retries_total", "counter", "Chat model retries by error type"),
        Metric("llm_fallbacks_total", "counter", "Calls moved to the fallback model"),
        Metric("embedding_requests_total", "counter", "Embedding calls by outcome"),
        Metric("embedding_duration_seconds", "histogram", "Embedding call duration"),
        Metric("ingestion_jobs_finished_total", "counter", "Job attempts by result"),
        Metric("ingestion_job_duration_seconds", "histogram", "Job attempt duration"),
        Metric("ingestion_queue_wait_seconds", "histogram", "Time from available to claimed"),
        Metric("ingestion_jobs_recovered_total", "counter", "Jobs recovered after a lost lease"),
        Metric("ingestion_jobs_handed_back_total", "counter", "Jobs handed back at shutdown"),
        Metric("usage_operations_total", "counter", "Finalised operations by outcome"),
        Metric("usage_tokens_total", "counter", "Charged tokens by operation and model"),
        Metric("usage_cost_usd_total", "counter", "Estimated cost by operation and model"),
        Metric("usage_reservations_expired_total", "counter", "Reservations expired by sweeps"),
    )
}

Labels = tuple[tuple[str, str], ...]


def _label(value: object) -> str:
    return _SAFE_LABEL.sub("_", str(value))[:80]


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[Labels, float]] = defaultdict(dict)
        self._histograms: dict[str, dict[Labels, list[float]]] = defaultdict(dict)

    def inc(self, name: str, value: float = 1.0, **labels: object) -> None:
        assert METRICS[name].kind == "counter", name
        key = tuple(sorted((k, _label(v)) for k, v in labels.items()))
        with self._lock:
            series = self._counters[name]
            series[key] = series.get(key, 0.0) + value

    def observe(self, name: str, seconds: float, **labels: object) -> None:
        assert METRICS[name].kind == "histogram", name
        if not math.isfinite(seconds):
            return
        key = tuple(sorted((k, _label(v)) for k, v in labels.items()))
        with self._lock:
            # Bucket counts, then sum and count.
            series = self._histograms[name].setdefault(key, [0.0] * (len(LATENCY_BUCKETS) + 2))
            for index, bound in enumerate(LATENCY_BUCKETS):
                if seconds <= bound:
                    series[index] += 1
            series[-2] += seconds
            series[-1] += 1

    def value(self, name: str, **labels: object) -> float:
        """For tests and diagnostics: a counter's value, or a histogram's count."""
        key = tuple(sorted((k, _label(v)) for k, v in labels.items()))
        with self._lock:
            if name in self._counters:
                return self._counters[name].get(key, 0.0)
            return self._histograms.get(name, {}).get(key, [0.0])[-1]

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    def render(self, gauges: dict[str, tuple[str, list[tuple[dict[str, object], float]]]]) -> str:
        lines: list[str] = []

        def fmt(labels: Labels | list[tuple[str, str]]) -> str:
            return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""

        with self._lock:
            for name, metric in METRICS.items():
                lines.append(f"# HELP {name} {metric.help}")
                lines.append(f"# TYPE {name} {metric.kind}")
                if metric.kind == "counter":
                    for labels, value in sorted(self._counters.get(name, {}).items()):
                        lines.append(f"{name}{fmt(labels)} {value:g}")
                    continue
                for labels, series in sorted(self._histograms.get(name, {}).items()):
                    for index, bound in enumerate(LATENCY_BUCKETS):
                        bucket = [*labels, ("le", f"{bound:g}")]
                        lines.append(f"{name}_bucket{fmt(bucket)} {series[index]:g}")
                    lines.append(f"{name}_bucket{fmt([*labels, ('le', '+Inf')])} {series[-1]:g}")
                    lines.append(f"{name}_sum{fmt(labels)} {series[-2]:.6f}")
                    lines.append(f"{name}_count{fmt(labels)} {series[-1]:g}")
        for name, (help_text, samples) in gauges.items():
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            for labels, value in samples:
                key = tuple(sorted((k, _label(v)) for k, v in labels.items()))
                lines.append(f"{name}{fmt(key)} {value:g}")
        return "\n".join(lines) + "\n"


metrics = Registry()
