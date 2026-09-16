#!/usr/bin/env python3
"""Benchmark harness comparing three multi-agent document-processing
architectures against the Gemini API.

Approach A ("extract once"): Agent 1 receives the original PDF/image and
extracts its text. Agents 2..N receive ONLY that extracted text.

Approach B ("reprocess"): every agent independently receives and reprocesses
the original PDF/image.

Approach C ("cache once, query many"): the document is loaded and cached
with the Gemini API exactly once; all agents make independent calls against
that same cache instead of re-uploading the document or sharing a transcript.
The cache is always deleted afterward, including when an agent call fails.

The approaches are measured, not designed around a task -- agent prompts are
deliberately simple/dummy (see agents_config.json) so what's being compared
is architecture cost (latency, token usage), not business logic quality. See
README.md for the full experiment design.
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
APPROACH_C = "C"

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

# Approach C's context cache only needs to outlive the 3 agent calls made
# against it; a short TTL keeps the (separately billed) cache-storage cost
# negligible, which is the point of the "short-lived" design.
CACHE_TTL_SECONDS = 60


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
    cached_content_token_count: Optional[int]
    start_time: datetime
    end_time: datetime


@dataclass
class CacheCreateResult:
    """Result of creating a short-lived context cache for one document.

    `cached_token_count` comes from the cache's own `usage_metadata`, i.e. the
    API's confirmation of how many tokens it actually stored -- not an
    assumption derived from file size or a nominal per-page estimate.
    """

    cache_name: Optional[str]
    success: bool
    error_message: Optional[str]
    create_seconds: float
    cached_token_count: Optional[int]


@dataclass
class CacheDeleteResult:
    success: bool
    error_message: Optional[str]
    delete_seconds: float


class GeminiClient:
    """Thin wrapper around google-genai so latency/usage capture happens in
    exactly one place regardless of which approach or agent is calling."""

    def __init__(self, api_key: str, model_name: str):
        self._client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def call(self, contents: list, cached_content: Optional[str] = None) -> GeminiCallResult:
        """Makes one generate_content call. When `cached_content` (a cache
        resource name from create_cache) is given, the call references that
        cache instead of resending document bytes in `contents`."""
        start_dt = datetime.now(timezone.utc)
        start = time.perf_counter()
        try:
            config = (
                types.GenerateContentConfig(cached_content=cached_content)
                if cached_content
                else None
            )
            response = self._client.models.generate_content(
                model=self.model_name, contents=contents, config=config
            )
            api_latency_seconds = time.perf_counter() - start
            end_dt = datetime.now(timezone.utc)

            usage = getattr(response, "usage_metadata", None)
            input_tokens = getattr(usage, "prompt_token_count", None) if usage else None
            output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
            total_tokens = getattr(usage, "total_token_count", None) if usage else None
            cached_tokens = getattr(usage, "cached_content_token_count", None) if usage else None

            return GeminiCallResult(
                text=response.text or "",
                success=True,
                error_message=None,
                api_latency_seconds=api_latency_seconds,
                input_token_count=input_tokens,
                output_token_count=output_tokens,
                total_token_count=total_tokens,
                cached_content_token_count=cached_tokens,
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
                cached_content_token_count=None,
                start_time=start_dt,
                end_time=end_dt,
            )

    def create_cache(self, data: bytes, mime_type: str, ttl_seconds: int) -> CacheCreateResult:
        """Creates a short-lived context cache holding one document."""
        start = time.perf_counter()
        try:
            cache = self._client.caches.create(
                model=self.model_name,
                config=types.CreateCachedContentConfig(
                    contents=[types.Part.from_bytes(data=data, mime_type=mime_type)],
                    ttl=f"{ttl_seconds}s",
                ),
            )
            cache_usage = getattr(cache, "usage_metadata", None)
            cached_token_count = getattr(cache_usage, "total_token_count", None) if cache_usage else None
            return CacheCreateResult(
                cache_name=cache.name,
                success=True,
                error_message=None,
                create_seconds=time.perf_counter() - start,
                cached_token_count=cached_token_count,
            )
        except Exception as exc:  # noqa: BLE001 - cache creation can fail in many API-specific ways
            logger.error("Cache creation failed: %s", exc)
            return CacheCreateResult(
                cache_name=None,
                success=False,
                error_message=str(exc),
                create_seconds=time.perf_counter() - start,
                cached_token_count=None,
            )

    def delete_cache(self, cache_name: str) -> CacheDeleteResult:
        """Best-effort cache deletion. Never raises -- callers must be able to
        unconditionally clean up from a `finally` block."""
        start = time.perf_counter()
        try:
            self._client.caches.delete(name=cache_name)
            return CacheDeleteResult(
                success=True, error_message=None, delete_seconds=time.perf_counter() - start
            )
        except Exception as exc:  # noqa: BLE001 - deletion can fail in many API-specific ways
            logger.error("Cache deletion failed for %s: %s", cache_name, exc)
            return CacheDeleteResult(
                success=False, error_message=str(exc), delete_seconds=time.perf_counter() - start
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
    cached_content_token_count: Optional[int] = None


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
        cached_content_token_count=result.cached_content_token_count,
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


@dataclass
class CacheMetrics:
    """One row per document/run for Approach C: the lifecycle of the one
    context cache shared by all of that document's agent calls."""

    document_name: str
    run_index: int
    cache_name: Optional[str]
    document_load_seconds: float
    create_seconds: float
    cache_active_seconds: float
    delete_seconds: float
    cached_token_count: Optional[int]
    create_success: bool
    delete_success: bool
    error_message: Optional[str]


def run_approach_c(
    document_path: Path,
    run_index: int,
    config: Config,
    agents: list[AgentDefinition],
    client: GeminiClient,
) -> tuple[list[CallMetrics], CacheMetrics]:
    """Cache once, query many: the document is loaded and cached exactly
    once, then every agent makes its own independent call against that same
    cache instead of resending the document or sharing a transcript.

    Agent 1 uses its plain `prompt` here, never `approach_a_prompt` -- like
    Approach B, nothing downstream consumes Agent 1's output (the document
    itself is shared via the cache, not via a transcript), so Agent 1 stays a
    narrow legibility check rather than a full-text dump.

    The cache is always deleted once the agents are done, even if one of them
    fails or an unexpected exception is raised, via `finally` -- a cache is a
    billed resource for as long as it exists, so cleanup must not depend on
    every call having succeeded.
    """
    document_name = document_path.name

    try:
        loaded = load_document(document_path)
    except Exception as exc:
        logger.error("Failed to load %s: %s", document_path, exc)
        error_message = f"document load failed: {exc}"
        return (
            [
                _skipped_call_metrics(
                    document_name, APPROACH_C, agent, run_index, config.model_name, error_message,
                )
                for agent in agents
            ],
            CacheMetrics(
                document_name=document_name, run_index=run_index, cache_name=None,
                document_load_seconds=0.0, create_seconds=0.0, cache_active_seconds=0.0,
                delete_seconds=0.0, cached_token_count=None, create_success=False,
                delete_success=True, error_message=error_message,
            ),
        )

    cache_result = client.create_cache(loaded.data, loaded.mime_type, CACHE_TTL_SECONDS)

    if not cache_result.success:
        error_message = f"cache creation failed: {cache_result.error_message}"
        return (
            [
                _skipped_call_metrics(
                    document_name, APPROACH_C, agent, run_index, config.model_name, error_message,
                )
                for agent in agents
            ],
            CacheMetrics(
                document_name=document_name, run_index=run_index, cache_name=None,
                document_load_seconds=loaded.load_seconds, create_seconds=cache_result.create_seconds,
                cache_active_seconds=0.0, delete_seconds=0.0, cached_token_count=None,
                create_success=False, delete_success=True, error_message=cache_result.error_message,
            ),
        )

    metrics: list[CallMetrics] = []
    active_start = time.perf_counter()
    delete_result: Optional[CacheDeleteResult] = None
    try:
        for agent in agents:
            contents = [agent.prompt]
            result = client.call(contents, cached_content=cache_result.cache_name)
            metrics.append(
                _call_metrics_from_result(
                    document_name, APPROACH_C, agent, run_index, config.model_name,
                    0.0, result,
                )
            )
    finally:
        cache_active_seconds = time.perf_counter() - active_start
        delete_result = client.delete_cache(cache_result.cache_name)

    cache_metrics = CacheMetrics(
        document_name=document_name,
        run_index=run_index,
        cache_name=cache_result.cache_name,
        document_load_seconds=loaded.load_seconds,
        create_seconds=cache_result.create_seconds,
        cache_active_seconds=cache_active_seconds,
        delete_seconds=delete_result.delete_seconds,
        cached_token_count=cache_result.cached_token_count,
        create_success=True,
        delete_success=delete_result.success,
        error_message=delete_result.error_message,
    )
    return metrics, cache_metrics


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
    total_cached_tokens: int
    num_gemini_calls: int
    num_failed_calls: int


def aggregate_per_document(
    document_name: str,
    approach: str,
    run_index: int,
    calls: list[CallMetrics],
    extra_latency_seconds: float = 0.0,
) -> DocumentSummary:
    """`extra_latency_seconds` covers architecture-specific overhead that
    isn't attached to any individual agent call, such as Approach C's cache
    create/delete round trips, so end-to-end latency stays comparable across
    approaches."""
    return DocumentSummary(
        document_name=document_name,
        approach=approach,
        run_index=run_index,
        total_end_to_end_latency_seconds=sum(c.latency_seconds for c in calls) + extra_latency_seconds,
        total_input_tokens=sum(c.input_token_count or 0 for c in calls),
        total_output_tokens=sum(c.output_token_count or 0 for c in calls),
        total_tokens=sum(c.total_token_count or 0 for c in calls),
        total_cached_tokens=sum(c.cached_content_token_count or 0 for c in calls),
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
    mean_cached_content_tokens: Optional[float] = None
    num_calls_with_confirmed_cache_hit: int = 0
    estimated_cache_storage_cost_usd: Optional[float] = None


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
    cache_metrics: Optional[list[CacheMetrics]] = None,
) -> ApproachStats:
    latencies = [c.latency_seconds for c in call_metrics]
    input_tokens = [c.input_token_count for c in call_metrics if c.input_token_count is not None]
    output_tokens = [c.output_token_count for c in call_metrics if c.output_token_count is not None]
    total_input_tokens = sum(input_tokens)
    total_output_tokens = sum(output_tokens)
    total_tokens = sum(c.total_token_count or 0 for c in call_metrics)
    num_documents = len({s.document_name for s in document_summaries})

    # Cache hits are reported only when the API's own usage_metadata confirms
    # them -- never assumed from cache creation succeeding or from a nominal
    # token estimate. `cached_content_token_count` is None on every call for
    # Approaches A/B and for any Approach C call the API didn't tag.
    cached_tokens_seen = [
        c.cached_content_token_count for c in call_metrics if c.cached_content_token_count is not None
    ]
    mean_cached_content_tokens = statistics.mean(cached_tokens_seen) if cached_tokens_seen else None
    num_calls_with_confirmed_cache_hit = sum(1 for t in cached_tokens_seen if t > 0)

    estimated_cost = None
    if pricing:
        estimated_cost = (
            total_input_tokens / 1_000_000 * pricing["input_cost_per_1m_tokens"]
            + total_output_tokens / 1_000_000 * pricing["output_cost_per_1m_tokens"]
        )

    # Cache storage cost is computed from each cache's own measured size
    # (cached_token_count, from the cache's usage_metadata) and measured
    # lifetime (cache_active_seconds) -- both API/clock facts, not estimates
    # -- so it's kept as a separate line item rather than folded into
    # estimated_cost above.
    estimated_cache_storage_cost = None
    if pricing and pricing.get("cache_storage_cost_per_1m_tokens_per_hour") and cache_metrics:
        rate_per_hour = pricing["cache_storage_cost_per_1m_tokens_per_hour"]
        total_storage_cost = 0.0
        for cm in cache_metrics:
            if cm.cached_token_count and cm.cache_active_seconds:
                total_storage_cost += (
                    cm.cached_token_count / 1_000_000 * rate_per_hour * (cm.cache_active_seconds / 3600)
                )
        estimated_cache_storage_cost = total_storage_cost

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
        mean_cached_content_tokens=mean_cached_content_tokens,
        num_calls_with_confirmed_cache_hit=num_calls_with_confirmed_cache_hit,
        estimated_cache_storage_cost_usd=estimated_cache_storage_cost,
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


def write_cache_metrics_csv(path: Path, cache_metrics: list[CacheMetrics]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(cache_metrics[0]).keys()))
        writer.writeheader()
        for cm in cache_metrics:
            writer.writerow(asdict(cm))


def print_final_report(stats_list: list[ApproachStats]) -> None:
    label_width = 26
    col_width = 16
    table_width = label_width + col_width * len(stats_list)

    def row(label: str, values: list, fmt: str = "{:.3f}") -> str:
        cells = [fmt.format(v) if isinstance(v, float) else str(v) for v in values]
        return f"{label:<{label_width}}" + "".join(f"{c:>{col_width}}" for c in cells)

    lines = [
        "",
        "=" * table_width,
        "APPROACH COMPARISON SUMMARY",
        "=" * table_width,
        row("", [f"Approach {s.approach}" for s in stats_list], "{}"),
        row("Documents", [s.num_documents for s in stats_list], "{}"),
        row("Agents", [s.num_agents for s in stats_list], "{}"),
        row("Runs per document", [s.num_runs for s in stats_list], "{}"),
        row("Gemini calls", [s.num_calls for s in stats_list], "{}"),
        row("Failed calls", [s.num_failed_calls for s in stats_list], "{}"),
        row("Avg latency (s)", [s.mean_latency_seconds for s in stats_list]),
        row("P50 latency (s)", [s.p50_latency_seconds for s in stats_list]),
        row("P95 latency (s)", [s.p95_latency_seconds for s in stats_list]),
        row("Avg input tokens", [s.mean_input_tokens for s in stats_list]),
        row("Avg output tokens", [s.mean_output_tokens for s in stats_list]),
        row("Total tokens", [s.total_tokens for s in stats_list], "{}"),
        row("Avg tokens/document", [s.avg_tokens_per_document for s in stats_list]),
        row(
            "Avg cached tokens/call",
            [s.mean_cached_content_tokens or 0.0 for s in stats_list],
        ),
        row(
            "Confirmed cache-hit calls",
            [s.num_calls_with_confirmed_cache_hit for s in stats_list],
            "{}",
        ),
    ]
    if any(s.estimated_cost_usd is not None for s in stats_list):
        lines.append(
            row("Est. LLM cost (USD)", [s.estimated_cost_usd or 0.0 for s in stats_list], "{:.4f}")
        )
    if any(s.estimated_cache_storage_cost_usd is not None for s in stats_list):
        lines.append(
            row(
                "Est. cache storage (USD)",
                [s.estimated_cache_storage_cost_usd or 0.0 for s in stats_list],
                "{:.6f}",
            )
        )
    lines.append("-" * table_width)
    lines.append("Pairwise percentage differences (positive = the second approach is higher):")
    for i in range(len(stats_list)):
        for j in range(i + 1, len(stats_list)):
            diffs = percentage_difference(stats_list[i], stats_list[j])
            lines.append(f"  {stats_list[j].approach} vs {stats_list[i].approach}:")
            for key, value in diffs.items():
                lines.append(f"    {key:<24} {value:+.1f}%")
    lines.append("=" * table_width)
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

    total_expected_calls = len(documents) * config.num_runs * config.num_agents * 3
    logger.info(
        "Starting experiment: %d document(s), %d agent(s), %d run(s)/document, "
        "3 approaches -> up to %d Gemini calls",
        len(documents), config.num_agents, config.num_runs, total_expected_calls,
    )

    all_calls: list[CallMetrics] = []
    all_summaries: list[DocumentSummary] = []
    all_cache_metrics: list[CacheMetrics] = []

    # Each approach runs sequentially across all documents/runs, per the
    # experiment's reproducibility requirement -- approaches are never
    # interleaved so nothing from one contaminates the other's measurement.
    for approach_name, runner in (
        (APPROACH_A, run_approach_a),
        (APPROACH_B, run_approach_b),
        (APPROACH_C, run_approach_c),
    ):
        for run_index in range(config.num_runs):
            for document_path in documents:
                logger.info(
                    "Approach %s, run %d/%d, document '%s'",
                    approach_name, run_index + 1, config.num_runs, document_path.name,
                )
                extra_latency_seconds = 0.0
                if approach_name == APPROACH_C:
                    calls, cache_metrics = runner(document_path, run_index, config, agents, client)
                    all_cache_metrics.append(cache_metrics)
                    extra_latency_seconds = (
                        cache_metrics.document_load_seconds
                        + cache_metrics.create_seconds
                        + cache_metrics.delete_seconds
                    )
                else:
                    calls = runner(document_path, run_index, config, agents, client)
                all_calls.extend(calls)
                all_summaries.append(
                    aggregate_per_document(
                        document_path.name, approach_name, run_index, calls, extra_latency_seconds
                    )
                )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    calls_csv_path = config.output_folder / f"gemini_calls_{timestamp}.csv"
    summary_csv_path = config.output_folder / f"document_summary_{timestamp}.csv"
    write_call_metrics_csv(calls_csv_path, all_calls)
    write_document_summary_csv(summary_csv_path, all_summaries)
    logger.info("Wrote %s and %s", calls_csv_path, summary_csv_path)

    if all_cache_metrics:
        cache_csv_path = config.output_folder / f"cache_metrics_{timestamp}.csv"
        write_cache_metrics_csv(cache_csv_path, all_cache_metrics)
        logger.info("Wrote %s", cache_csv_path)

    stats_list = []
    for approach_name in (APPROACH_A, APPROACH_B, APPROACH_C):
        calls = [c for c in all_calls if c.approach == approach_name]
        summaries = [s for s in all_summaries if s.approach == approach_name]
        cache_metrics = all_cache_metrics if approach_name == APPROACH_C else None
        stats_list.append(
            aggregate_overall(
                approach_name, config.num_agents, config.num_runs, summaries, calls, pricing, cache_metrics
            )
        )

    print_final_report(stats_list)


if __name__ == "__main__":
    main()
