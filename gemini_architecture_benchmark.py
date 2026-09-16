#!/usr/bin/env python3
"""Benchmark harness comparing two multi-agent document-processing architectures
against the Gemini API.

Approach A ("extract once"): Agent 1 receives the original PDF/image and
extracts its text. Agents 2..N receive ONLY that extracted text.

Approach B ("reprocess"): every agent independently receives and reprocesses
the original PDF/image.

The two approaches are measured, not designed around a task -- agent prompts
are deliberately simple/dummy (see agents_config.json) so what's being
compared is architecture cost (latency, token usage), not business logic
quality. See goal.txt and README.md for the full experiment design.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import mimetypes
import os
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

logger = logging.getLogger("gemini_architecture_benchmark")

APPROACH_A = "A"
APPROACH_B = "B"

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class Config:
    """Runtime configuration. Credentials come from the environment only --
    never accepted as a CLI flag, so a key can't end up in shell history."""

    gemini_api_key: str
    model_name: str
    input_folder: Path
    output_folder: Path
    num_agents: int
    num_runs: int
    agents_config_path: Path
    pricing_config_path: Optional[Path]
    log_level: str


def load_config(argv: Optional[list[str]] = None) -> Config:
    """Builds Config from environment variables (optionally via a .env file),
    with CLI flags overriding the corresponding environment variable."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Compare Gemini 'extract once' vs 'reprocess' document-processing architectures."
    )
    parser.add_argument("--input-folder", default=os.environ.get("INPUT_FOLDER", "dc_data"))
    parser.add_argument("--output-folder", default=os.environ.get("OUTPUT_FOLDER", "output"))
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"))
    parser.add_argument("--num-agents", type=int, default=int(os.environ.get("NUM_AGENTS", "3")))
    parser.add_argument("--num-runs", type=int, default=int(os.environ.get("NUM_RUNS", "1")))
    parser.add_argument(
        "--agents-config", default=os.environ.get("AGENTS_CONFIG_PATH", "agents_config.json")
    )
    parser.add_argument("--pricing-config", default=os.environ.get("PRICING_CONFIG_PATH") or None)
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit(
            "GEMINI_API_KEY environment variable is required. "
            "Set it in a .env file or your shell environment; see .env.example."
        )

    return Config(
        gemini_api_key=api_key,
        model_name=args.model,
        input_folder=Path(args.input_folder),
        output_folder=Path(args.output_folder),
        num_agents=args.num_agents,
        num_runs=args.num_runs,
        agents_config_path=Path(args.agents_config),
        pricing_config_path=Path(args.pricing_config) if args.pricing_config else None,
        log_level=args.log_level,
    )


# --------------------------------------------------------------------------
# Agent definitions
# --------------------------------------------------------------------------


@dataclass
class AgentDefinition:
    """`prompt` is the agent's standard, narrow task -- used as-is in
    Approach B (every agent asks its own targeted question of the original
    document) and by any Approach A agent that isn't first in the chain.

    `approach_a_prompt` is an optional override used only by the first agent
    in Approach A, whose job is structurally different: it must produce the
    full text that downstream agents will consume, not just answer a narrow
    question. Without this override, Approach A's Agent 1 would reuse the
    Approach B prompt and never produce reusable extracted text; using the
    same "extract everything verbatim" prompt for Approach B's Agent 1 would
    inflate its output tokens for no reason, since nothing downstream in B
    consumes that text.
    """

    agent_number: int
    name: str
    prompt: str
    approach_a_prompt: Optional[str] = None


def load_agents(agents_config_path: Path, num_agents: int) -> list[AgentDefinition]:
    """Loads agent name/prompt definitions from JSON and returns the first
    `num_agents` of them (sorted by agent_number), so a single config file can
    define more agents than any given run uses."""
    if not agents_config_path.exists():
        raise SystemExit(f"Agents config file not found: {agents_config_path}")

    raw = json.loads(agents_config_path.read_text())
    agents = sorted(
        (AgentDefinition(**item) for item in raw), key=lambda a: a.agent_number
    )

    if len(agents) < num_agents:
        raise SystemExit(
            f"{agents_config_path} defines {len(agents)} agent(s), but "
            f"--num-agents={num_agents} was requested. Add more agents to the "
            f"config file or lower --num-agents."
        )
    return agents[:num_agents]


# --------------------------------------------------------------------------
# Document handling
# --------------------------------------------------------------------------


@dataclass
class LoadedDocument:
    data: bytes
    mime_type: str
    load_seconds: float


def discover_documents(folder: Path) -> list[Path]:
    if not folder.exists():
        raise SystemExit(f"Input folder not found: {folder}")
    documents = sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not documents:
        raise SystemExit(f"No supported documents ({SUPPORTED_EXTENSIONS}) found in {folder}")
    return documents


def load_document(path: Path) -> LoadedDocument:
    """Reads a document from disk, measuring load time with a perf_counter.

    Deliberately not cached: Approach B calls this once per agent so the cost
    of repeatedly loading/parsing the original document is captured rather
    than hidden, per the experiment's explicit measurement goal.
    """
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type is None:
        raise ValueError(f"Could not determine mime type for {path}")

    start = time.perf_counter()
    data = path.read_bytes()
    load_seconds = time.perf_counter() - start
    return LoadedDocument(data=data, mime_type=mime_type, load_seconds=load_seconds)


# --------------------------------------------------------------------------
# Gemini client wrapper
# --------------------------------------------------------------------------


@dataclass
class GeminiCallResult:
    text: str
    success: bool
    error_message: Optional[str]
    api_latency_seconds: float
    input_token_count: Optional[int]
    output_token_count: Optional[int]
    total_token_count: Optional[int]
    start_time: datetime
    end_time: datetime


class GeminiClient:
    """Thin wrapper around google-genai so latency/usage capture happens in
    exactly one place regardless of which approach or agent is calling."""

    def __init__(self, api_key: str, model_name: str):
        self._client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def call(self, contents: list) -> GeminiCallResult:
        start_dt = datetime.now(timezone.utc)
        start = time.perf_counter()
        try:
            response = self._client.models.generate_content(
                model=self.model_name, contents=contents
            )
            api_latency_seconds = time.perf_counter() - start
            end_dt = datetime.now(timezone.utc)

            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
            output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
            total_tokens = getattr(usage, "total_token_count", None) if usage else None

            return GeminiCallResult(
                text=response.text or "",
                success=True,
                error_message=None,
                api_latency_seconds=api_latency_seconds,
                input_token_count=input_tokens,
                output_token_count=output_tokens,
                total_token_count=total_tokens,
                start_time=start_dt,
                end_time=end_dt,
            )
        except Exception as exc:  # noqa: BLE001 - Gemini calls can fail in many API-specific ways
            api_latency_seconds = time.perf_counter() - start
            end_dt = datetime.now(timezone.utc)
            logger.error("Gemini call failed: %s", exc)
            return GeminiCallResult(
                text="",
                success=False,
                error_message=str(exc),
                api_latency_seconds=api_latency_seconds,
                input_token_count=None,
                output_token_count=None,
                total_token_count=None,
                start_time=start_dt,
                end_time=end_dt,
            )


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


@dataclass
class CallMetrics:
    """One row per Gemini API call, per the experiment's metrics spec."""

    document_name: str
    approach: str
    agent_name: str
    agent_number: int
    run_index: int
    start_time: str
    end_time: str
    latency_seconds: float
    document_load_seconds: float
    gemini_api_latency_seconds: float
    total_agent_latency_seconds: float
    input_token_count: Optional[int]
    output_token_count: Optional[int]
    total_token_count: Optional[int]
    model_name: str
    success: bool
    error_message: Optional[str]


def _skipped_call_metrics(
    document_name: str,
    approach: str,
    agent: AgentDefinition,
    run_index: int,
    model_name: str,
    error_message: str,
) -> CallMetrics:
    """Records an agent step that never made an API call (e.g. upstream
    extraction failed, or the document itself failed to load) -- failures
    must be recorded, never silently dropped."""
    now = datetime.now(timezone.utc).isoformat()
    return CallMetrics(
        document_name=document_name,
        approach=approach,
        agent_name=agent.name,
        agent_number=agent.agent_number,
        run_index=run_index,
        start_time=now,
        end_time=now,
        latency_seconds=0.0,
        document_load_seconds=0.0,
        gemini_api_latency_seconds=0.0,
        total_agent_latency_seconds=0.0,
        input_token_count=None,
        output_token_count=None,
        total_token_count=None,
        model_name=model_name,
        success=False,
        error_message=error_message,
    )


def _call_metrics_from_result(
    document_name: str,
    approach: str,
    agent: AgentDefinition,
    run_index: int,
    model_name: str,
    document_load_seconds: float,
    result: GeminiCallResult,
) -> CallMetrics:
    total_latency = document_load_seconds + result.api_latency_seconds
    return CallMetrics(
        document_name=document_name,
        approach=approach,
        agent_name=agent.name,
        agent_number=agent.agent_number,
        run_index=run_index,
        start_time=result.start_time.isoformat(),
        end_time=result.end_time.isoformat(),
        latency_seconds=total_latency,
        document_load_seconds=document_load_seconds,
        gemini_api_latency_seconds=result.api_latency_seconds,
        total_agent_latency_seconds=total_latency,
        input_token_count=result.input_token_count,
        output_token_count=result.output_token_count,
        total_token_count=result.total_token_count,
        model_name=model_name,
        success=result.success,
        error_message=result.error_message,
    )


# --------------------------------------------------------------------------
# Experiment runners
# --------------------------------------------------------------------------


def run_approach_a(
    document_path: Path,
    run_index: int,
    config: Config,
    agents: list[AgentDefinition],
    client: GeminiClient,
) -> list[CallMetrics]:
    """Extract once: Agent 1 sees the raw document; Agents 2..N see only the
    text Agent 1 returned, reused verbatim (no re-extraction, no editing)."""
    document_name = document_path.name
    metrics: list[CallMetrics] = []

    try:
        loaded = load_document(document_path)
    except Exception as exc:
        logger.error("Failed to load %s: %s", document_path, exc)
        return [
            _skipped_call_metrics(
                document_name, APPROACH_A, agent, run_index, config.model_name,
                f"document load failed: {exc}",
            )
            for agent in agents
        ]

    extracted_text: Optional[str] = None
    for index, agent in enumerate(agents):
        if index == 0:
            # First agent in the chain: multimodal input, original document.
            # Uses approach_a_prompt (full verbatim extraction) when defined,
            # since this agent's output becomes the shared state every
            # downstream agent depends on -- see AgentDefinition docstring.
            prompt_text = agent.approach_a_prompt or agent.prompt
            contents = [types.Part.from_bytes(data=loaded.data, mime_type=loaded.mime_type), prompt_text]
            result = client.call(contents)
            metrics.append(
                _call_metrics_from_result(
                    document_name, APPROACH_A, agent, run_index, config.model_name,
                    loaded.load_seconds, result,
                )
            )
            extracted_text = result.text if result.success else None
        elif extracted_text is None:
            # Upstream extraction failed (or never ran) -- record the skip,
            # continue with the rest of the chain rather than aborting.
            metrics.append(
                _skipped_call_metrics(
                    document_name, APPROACH_A, agent, run_index, config.model_name,
                    "skipped: upstream extraction failed",
                )
            )
        else:
            contents = [f"{agent.prompt}\n\n--- Document text (extracted by a prior step) ---\n{extracted_text}"]
            result = client.call(contents)
            metrics.append(
                _call_metrics_from_result(
                    document_name, APPROACH_A, agent, run_index, config.model_name,
                    0.0, result,
                )
            )
    return metrics


def run_approach_b(
    document_path: Path,
    run_index: int,
    config: Config,
    agents: list[AgentDefinition],
    client: GeminiClient,
) -> list[CallMetrics]:
    """Reprocess: every agent independently loads and receives the original
    document. Loading is repeated on purpose -- see load_document()."""
    document_name = document_path.name
    metrics: list[CallMetrics] = []

    for agent in agents:
        try:
            loaded = load_document(document_path)
        except Exception as exc:
            logger.error("Failed to load %s: %s", document_path, exc)
            metrics.append(
                _skipped_call_metrics(
                    document_name, APPROACH_B, agent, run_index, config.model_name,
                    f"document load failed: {exc}",
                )
            )
            continue

        contents = [types.Part.from_bytes(data=loaded.data, mime_type=loaded.mime_type), agent.prompt]
        result = client.call(contents)
        metrics.append(
            _call_metrics_from_result(
                document_name, APPROACH_B, agent, run_index, config.model_name,
                loaded.load_seconds, result,
            )
        )
    return metrics


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


@dataclass
class DocumentSummary:
    document_name: str
    approach: str
    run_index: int
    total_end_to_end_latency_seconds: float
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    num_gemini_calls: int
    num_failed_calls: int


def aggregate_per_document(
    document_name: str, approach: str, run_index: int, calls: list[CallMetrics]
) -> DocumentSummary:
    return DocumentSummary(
        document_name=document_name,
        approach=approach,
        run_index=run_index,
        total_end_to_end_latency_seconds=sum(c.latency_seconds for c in calls),
        total_input_tokens=sum(c.input_token_count or 0 for c in calls),
        total_output_tokens=sum(c.output_token_count or 0 for c in calls),
        total_tokens=sum(c.total_token_count or 0 for c in calls),
        num_gemini_calls=len(calls),
        num_failed_calls=sum(1 for c in calls if not c.success),
    )


@dataclass
class ApproachStats:
    approach: str
    num_documents: int
    num_agents: int
    num_runs: int
    num_calls: int
    num_failed_calls: int
    mean_latency_seconds: float
    p50_latency_seconds: float
    p95_latency_seconds: float
    mean_input_tokens: float
    mean_output_tokens: float
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    avg_tokens_per_document: float
    estimated_cost_usd: Optional[float] = None


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile; avoids a numpy dependency for a
    single-purpose stat. Returns 0.0 for an empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100)
    lower, upper = int(k), min(int(k) + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    fraction = k - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def aggregate_overall(
    approach: str,
    num_agents: int,
    num_runs: int,
    document_summaries: list[DocumentSummary],
    call_metrics: list[CallMetrics],
    pricing: Optional[dict] = None,
) -> ApproachStats:
    latencies = [c.latency_seconds for c in call_metrics]
    input_tokens = [c.input_token_count for c in call_metrics if c.input_token_count is not None]
    output_tokens = [c.output_token_count for c in call_metrics if c.output_token_count is not None]
    total_input_tokens = sum(input_tokens)
    total_output_tokens = sum(output_tokens)
    total_tokens = sum(c.total_token_count or 0 for c in call_metrics)
    num_documents = len({s.document_name for s in document_summaries})

    estimated_cost = None
    if pricing:
        estimated_cost = (
            total_input_tokens / 1_000_000 * pricing["input_cost_per_1m_tokens"]
            + total_output_tokens / 1_000_000 * pricing["output_cost_per_1m_tokens"]
        )

    return ApproachStats(
        approach=approach,
        num_documents=num_documents,
        num_agents=num_agents,
        num_runs=num_runs,
        num_calls=len(call_metrics),
        num_failed_calls=sum(1 for c in call_metrics if not c.success),
        mean_latency_seconds=statistics.mean(latencies) if latencies else 0.0,
        p50_latency_seconds=_percentile(latencies, 50),
        p95_latency_seconds=_percentile(latencies, 95),
        mean_input_tokens=statistics.mean(input_tokens) if input_tokens else 0.0,
        mean_output_tokens=statistics.mean(output_tokens) if output_tokens else 0.0,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_tokens=total_tokens,
        avg_tokens_per_document=total_tokens / num_documents if num_documents else 0.0,
        estimated_cost_usd=estimated_cost,
    )


def percentage_difference(a: ApproachStats, b: ApproachStats) -> dict[str, float]:
    """(B - A) / A * 100 for each headline metric. NaN when A's value is 0."""

    def diff(a_val: float, b_val: float) -> float:
        if a_val == 0:
            return float("nan") if b_val != 0 else 0.0
        return (b_val - a_val) / a_val * 100

    return {
        "mean_latency_seconds": diff(a.mean_latency_seconds, b.mean_latency_seconds),
        "p50_latency_seconds": diff(a.p50_latency_seconds, b.p50_latency_seconds),
        "p95_latency_seconds": diff(a.p95_latency_seconds, b.p95_latency_seconds),
        "mean_input_tokens": diff(a.mean_input_tokens, b.mean_input_tokens),
        "mean_output_tokens": diff(a.mean_output_tokens, b.mean_output_tokens),
        "total_tokens": diff(a.total_tokens, b.total_tokens),
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_call_metrics_csv(path: Path, calls: list[CallMetrics]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(calls[0]).keys()))
        writer.writeheader()
        for call in calls:
            writer.writerow(asdict(call))


def write_document_summary_csv(path: Path, summaries: list[DocumentSummary]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(summaries[0]).keys()))
        writer.writeheader()
        for summary in summaries:
            writer.writerow(asdict(summary))


def print_final_report(stats_a: ApproachStats, stats_b: ApproachStats, diffs: dict[str, float]) -> None:
    def row(label: str, a_val, b_val, fmt: str = "{:.3f}") -> str:
        a_str = fmt.format(a_val) if isinstance(a_val, float) else str(a_val)
        b_str = fmt.format(b_val) if isinstance(b_val, float) else str(b_val)
        return f"{label:<20} {a_str:>18} {b_str:>18}"

    lines = [
        "",
        "=" * 60,
        "APPROACH A vs APPROACH B - COMPARISON SUMMARY",
        "=" * 60,
        f"{'':<20} {'Approach A':>18} {'Approach B':>18}",
        row("Documents", stats_a.num_documents, stats_b.num_documents, "{}"),
        row("Agents", stats_a.num_agents, stats_b.num_agents, "{}"),
        row("Runs per document", stats_a.num_runs, stats_b.num_runs, "{}"),
        row("Gemini calls", stats_a.num_calls, stats_b.num_calls, "{}"),
        row("Failed calls", stats_a.num_failed_calls, stats_b.num_failed_calls, "{}"),
        row("Avg latency (s)", stats_a.mean_latency_seconds, stats_b.mean_latency_seconds),
        row("P50 latency (s)", stats_a.p50_latency_seconds, stats_b.p50_latency_seconds),
        row("P95 latency (s)", stats_a.p95_latency_seconds, stats_b.p95_latency_seconds),
        row("Avg input tokens", stats_a.mean_input_tokens, stats_b.mean_input_tokens),
        row("Avg output tokens", stats_a.mean_output_tokens, stats_b.mean_output_tokens),
        row("Total tokens", stats_a.total_tokens, stats_b.total_tokens, "{}"),
        row("Avg tokens/document", stats_a.avg_tokens_per_document, stats_b.avg_tokens_per_document),
    ]
    if stats_a.estimated_cost_usd is not None:
        lines.append(row("Est. cost (USD)", stats_a.estimated_cost_usd, stats_b.estimated_cost_usd, "{:.4f}"))
    lines.append("-" * 60)
    lines.append("Percentage difference, B vs A (positive = B is higher):")
    for key, value in diffs.items():
        lines.append(f"  {key:<24} {value:+.1f}%")
    lines.append("=" * 60)
    print("\n".join(lines))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    config = load_config(argv)
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(message)s")

    agents = load_agents(config.agents_config_path, config.num_agents)
    documents = discover_documents(config.input_folder)
    config.output_folder.mkdir(parents=True, exist_ok=True)

    pricing = None
    if config.pricing_config_path:
        pricing = json.loads(config.pricing_config_path.read_text())

    client = GeminiClient(api_key=config.gemini_api_key, model_name=config.model_name)

    total_expected_calls = len(documents) * config.num_runs * config.num_agents * 2
    logger.info(
        "Starting experiment: %d document(s), %d agent(s), %d run(s)/document, "
        "2 approaches -> up to %d Gemini calls",
        len(documents), config.num_agents, config.num_runs, total_expected_calls,
    )

    all_calls: list[CallMetrics] = []
    all_summaries: list[DocumentSummary] = []

    # Each approach runs sequentially across all documents/runs, per the
    # experiment's reproducibility requirement -- approaches are never
    # interleaved so nothing from one contaminates the other's measurement.
    for approach_name, runner in ((APPROACH_A, run_approach_a), (APPROACH_B, run_approach_b)):
        for run_index in range(config.num_runs):
            for document_path in documents:
                logger.info(
                    "Approach %s, run %d/%d, document '%s'",
                    approach_name, run_index + 1, config.num_runs, document_path.name,
                )
                calls = runner(document_path, run_index, config, agents, client)
                all_calls.extend(calls)
                all_summaries.append(
                    aggregate_per_document(document_path.name, approach_name, run_index, calls)
                )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    calls_csv_path = config.output_folder / f"gemini_calls_{timestamp}.csv"
    summary_csv_path = config.output_folder / f"document_summary_{timestamp}.csv"
    write_call_metrics_csv(calls_csv_path, all_calls)
    write_document_summary_csv(summary_csv_path, all_summaries)
    logger.info("Wrote %s and %s", calls_csv_path, summary_csv_path)

    calls_a = [c for c in all_calls if c.approach == APPROACH_A]
    calls_b = [c for c in all_calls if c.approach == APPROACH_B]
    summaries_a = [s for s in all_summaries if s.approach == APPROACH_A]
    summaries_b = [s for s in all_summaries if s.approach == APPROACH_B]

    stats_a = aggregate_overall(APPROACH_A, config.num_agents, config.num_runs, summaries_a, calls_a, pricing)
    stats_b = aggregate_overall(APPROACH_B, config.num_agents, config.num_runs, summaries_b, calls_b, pricing)
    diffs = percentage_difference(stats_a, stats_b)

    print_final_report(stats_a, stats_b, diffs)


if __name__ == "__main__":
    main()
