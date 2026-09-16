# Gemini Document-Processing Architecture Benchmark

Compares three multi-agent document-processing architectures against the
Gemini API, measuring latency and token usage -- not output quality. See
`EXPLAINER.md` for the full experiment write-up.

- **Approach A (extract once)**: Agent 1 receives the original PDF/image and
  extracts its text. Agents 2..N receive only that extracted text.
- **Approach B (reprocess)**: every agent independently receives and
  reprocesses the original PDF/image.
- **Approach C (cache once, query many)**: the document is loaded and cached
  with the Gemini API exactly once; every agent makes an independent call
  against that same cache instead of re-uploading the document or sharing a
  transcript. The cache is deleted once all agents are done, including when
  a call fails.

Agents are intentionally simple/dummy (document extraction, classification,
schema extraction by default) -- see `agents_config.json`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in GEMINI_API_KEY
```

`GEMINI_API_KEY` is required and read from the environment only -- it is
never accepted as a CLI flag and never hardcoded.

### A note on model access

The experiment is designed around `gemini-2.5-pro`. As of late 2026, Google
has deprecated the 2.5 model family for new API keys and moved all
Pro-tier models behind billing (see `gemini-3.1-pro-preview`, the current
flagship, which returns `429` with zero free-tier quota until billing is
enabled on the linked Cloud project).

`GEMINI_MODEL` is fully configurable, so:

- If your key predates the 2.5 deprecation, or you enable billing, set
  `GEMINI_MODEL` to the Pro-tier model you have access to (e.g.
  `gemini-2.5-pro` or `gemini-3.1-pro-preview`).
- Otherwise, `gemini-3.5-flash-lite` works on the free tier and is the
  default in `.env.example`, so the harness runs out of the box. Swap the
  model once you have Pro access -- no code changes needed.

Run `test_gemini_api.ipynb` first for a quick sanity check of your key/model
combination before running the full benchmark.

Context caching (Approach C) has its own model/tier requirements from Google
that are independent of the free-tier limits above -- some models impose a
minimum cached-content size, and caching itself may require a
billing-enabled project. If cache creation fails for your key/model/document
combination, Approach C records all three agents for that document/run as
skipped (see "Error handling" below) rather than the run crashing; Approaches
A and B are unaffected either way.

## Running

```bash
python gemini_architecture_benchmark.py
```

Configurable via environment variables (`.env`) or CLI flags (flags win):

| Env var | CLI flag | Default | Meaning |
|---|---|---|---|
| `GEMINI_MODEL` | `--model` | `gemini-2.5-pro` | Model name passed to the Gemini API |
| `INPUT_FOLDER` | `--input-folder` | `dc_data` | Folder of PDFs/images to process |
| `OUTPUT_FOLDER` | `--output-folder` | `output` | Where CSVs are written |
| `NUM_AGENTS` | `--num-agents` | `3` | Number of agents in the chain (must be `<=` agents defined in the agents config) |
| `NUM_RUNS` | `--num-runs` | `1` | Repetitions per document, for variability across runs |
| `AGENTS_CONFIG_PATH` | `--agents-config` | `agents_config.json` | Agent name/prompt definitions |
| `PRICING_CONFIG_PATH` | `--pricing-config` | unset | Optional cost estimate (see `pricing_config.example.json`) |
| `LOG_LEVEL` | `--log-level` | `INFO` | Python logging level |

Supported document types: `.pdf`, `.png`, `.jpg`, `.jpeg`.

## Agent configuration

`agents_config.json` is a JSON array of `{agent_number, name, prompt}`
objects, plus an optional `approach_a_prompt` on Agent 1. The default ships
3 agents (extraction/validation, classification, schema extraction).

Agents 2..N use the same `prompt` in all three approaches -- the harness
supplies the extracted text (A), the re-uploaded raw document (B), or a
reference to the shared cache (C) alongside it, so the instruction itself
stays equivalent across approaches, and each one asks its own narrow,
targeted question (e.g. "classify this" or "extract these fields as JSON")
regardless of which approach it's running under.

Agent 1 is the one place Approach A is structurally different from the other
two: in Approach A it must produce the full text every downstream agent will
consume, so it needs its own `approach_a_prompt` (full verbatim extraction).
In both Approach B and Approach C it has no downstream text consumer, so its
plain `prompt` should be a narrow, independent check (the default asks it to
validate the document is legible) -- not a full-text dump, which would
inflate its output tokens for no reason. Approach C's Agent 1 is not a
special case; it uses the same plain `prompt` as B's. If `approach_a_prompt`
is omitted, Approach A falls back to using `prompt` for Agent 1 too.

If you request more agents via `--num-agents` than the config file defines,
the script exits with an error rather than silently reusing/skipping agents.

## Output

Each run writes two timestamped CSVs to the output folder, plus a third one
for Approach C:

- `gemini_calls_<timestamp>.csv` -- one row per Gemini API call: document
  name, approach, agent, run index, start/end time, latency broken down into
  document-load time / Gemini API time / total agent time, input/output/total
  token counts, `cached_content_token_count` (populated only when the API's
  `usage_metadata` confirms a cache hit -- always empty for A/B), model name,
  success flag, and error message if any.
- `document_summary_<timestamp>.csv` -- one row per (document, approach, run):
  total end-to-end latency, total input/output/total/cached tokens, number of
  Gemini calls, number of failures.
- `cache_metrics_<timestamp>.csv` -- Approach C only, one row per
  document/run: the cache's name, document-load/create/active/delete
  durations, the cache's own confirmed token count (from the cache's
  `usage_metadata`), and whether creation/deletion succeeded.

A final comparison table (all three approaches: documents, agents, avg/P50/P95
latency, avg input/output tokens, total tokens, confirmed cache hits, and
pairwise percentage differences) is printed to stdout at the end of the run.

## Error handling

If an agent call fails, the failure and any latency/token data captured up
to that point are recorded (never silently dropped) and the experiment
continues with the next agent/document. In Approach A, if Agent 1 fails,
Agents 2..N for that document/run are recorded as skipped (no extracted
text available) rather than making a doomed API call. In Approach C, if
cache creation fails, all agents for that document/run are recorded as
skipped the same way; if cache creation succeeds but an agent call fails,
the other agents still run (they're independent), and the cache is deleted
regardless, from a `finally` block, so a failed run never leaves a paid
cache resource lingering. Cache deletion failures are themselves recorded,
not raised.

## Reproducibility

All three approaches run against the same documents and equivalent prompts,
each approach fully sequential across all documents/runs (never
interleaved). Set `--num-runs` above 1 to repeat each document multiple
times per approach and measure latency/token variability.

## Pricing

No token pricing is assumed by default -- the experiment measures actual
token usage and latency. To get an optional cost estimate, copy
`pricing_config.example.json`, fill in your own current per-1M-token
input/output prices, and pass `--pricing-config <path>`. Add an optional
`cache_storage_cost_per_1m_tokens_per_hour` key to the same file to also get
an Approach C cache-storage cost estimate, computed from each cache's
measured size and active duration -- never assumed from a nominal token
count.

## Testing

`test_approach_c.py` validates Approach C's cache lifecycle -- creation,
reuse across all three agent calls, unconditional cleanup, and both
cache-creation and cache-deletion failure handling -- against a mocked
`google.genai.Client`, so it runs without a live API key:

```bash
python3 -m unittest test_approach_c.py -v
```
