#!/usr/bin/env python3
"""Mocked validation for Approach C's cache lifecycle.

No network calls and no GEMINI_API_KEY are needed: GeminiClient._client (the
underlying google-genai Client) is replaced with a MagicMock, so every
caches.create / caches.delete / models.generate_content call is intercepted
and asserted against directly. This validates the *mechanics* Approach C
depends on -- cache creation, reuse across all three agents, cleanup, and
failure handling -- not the actual cost/latency numbers, which require a
live run against the real API.

Run with: python3 -m unittest test_approach_c.py -v
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from gemini_architecture_benchmark import (
    APPROACH_C,
    CACHE_TTL_SECONDS,
    AgentDefinition,
    Config,
    GeminiClient,
    run_approach_c,
)

AGENTS = [
    AgentDefinition(
        agent_number=1,
        name="document_extraction",
        prompt="Check legibility.",
        approach_a_prompt="Extract all text verbatim.",
    ),
    AgentDefinition(agent_number=2, name="document_classification", prompt="Classify this document."),
    AgentDefinition(agent_number=3, name="schema_extraction", prompt="Extract fields as JSON."),
]

DOCUMENT_PATH = Path("dc_data/dc1.png")


def make_usage(input_tokens=1149, output_tokens=24, cached_tokens=None):
    return SimpleNamespace(
        prompt_token_count=input_tokens,
        candidates_token_count=output_tokens,
        total_token_count=input_tokens + output_tokens,
        cached_content_token_count=cached_tokens,
    )


def make_response(text="{}", usage=None):
    return SimpleNamespace(text=text, usage_metadata=usage or make_usage())


def make_config() -> Config:
    return Config(
        gemini_api_key="test-key",
        model_name="gemini-test-model",
        input_folder=Path("dc_data"),
        output_folder=Path("output"),
        num_agents=3,
        num_runs=1,
        agents_config_path=Path("agents_config.json"),
        pricing_config_path=None,
        log_level="CRITICAL",
    )


class ApproachCTests(unittest.TestCase):
    def setUp(self):
        self.client = GeminiClient(api_key="test-key", model_name="gemini-test-model")
        self.mock_genai = MagicMock()
        self.client._client = self.mock_genai
        self.mock_genai.caches.create.return_value = SimpleNamespace(
            name="cachedContents/fake-cache-1",
            usage_metadata=SimpleNamespace(total_token_count=1149),
        )
        self.mock_genai.models.generate_content.return_value = make_response()
        self.config = make_config()

    def test_cache_created_exactly_once_and_reused_across_all_three_calls(self):
        calls, cache_metrics = run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        self.mock_genai.caches.create.assert_called_once()
        self.assertEqual(self.mock_genai.models.generate_content.call_count, 3)
        for _, kwargs in self.mock_genai.models.generate_content.call_args_list:
            self.assertEqual(kwargs["config"].cached_content, "cachedContents/fake-cache-1")

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c.success for c in calls))
        self.assertTrue(all(c.approach == APPROACH_C for c in calls))
        self.assertTrue(cache_metrics.create_success)
        self.assertEqual(cache_metrics.cache_name, "cachedContents/fake-cache-1")

    def test_agent_one_uses_narrow_prompt_not_full_transcript_prompt(self):
        run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        first_call_contents = self.mock_genai.models.generate_content.call_args_list[0].kwargs["contents"]
        self.assertIn("Check legibility.", first_call_contents)
        self.assertNotIn("Extract all text verbatim.", first_call_contents)

    def test_cache_ttl_matches_configured_short_lifetime(self):
        run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        create_config = self.mock_genai.caches.create.call_args.kwargs["config"]
        self.assertEqual(create_config.ttl, f"{CACHE_TTL_SECONDS}s")

    def test_cache_deleted_after_all_agents_succeed(self):
        run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        self.mock_genai.caches.delete.assert_called_once_with(name="cachedContents/fake-cache-1")

    def test_cache_still_deleted_when_a_middle_agent_call_fails(self):
        self.mock_genai.models.generate_content.side_effect = [
            make_response(),
            RuntimeError("simulated Gemini 500"),
            make_response(),
        ]

        calls, cache_metrics = run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        self.mock_genai.caches.delete.assert_called_once_with(name="cachedContents/fake-cache-1")
        self.assertTrue(cache_metrics.delete_success)
        self.assertEqual([c.success for c in calls], [True, False, True])
        self.assertIn("simulated Gemini 500", calls[1].error_message)
        # The other two agents are independent and still ran, matching how
        # Approach B treats agents as independent of each other's outcome.
        self.assertEqual(self.mock_genai.models.generate_content.call_count, 3)

    def test_cache_creation_failure_skips_all_agents_and_never_calls_delete(self):
        self.mock_genai.caches.create.side_effect = RuntimeError("document too small to cache")

        calls, cache_metrics = run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(not c.success for c in calls))
        self.assertTrue(all("cache creation failed" in c.error_message for c in calls))
        self.mock_genai.models.generate_content.assert_not_called()
        self.mock_genai.caches.delete.assert_not_called()
        self.assertFalse(cache_metrics.create_success)
        self.assertIsNone(cache_metrics.cache_name)

    def test_cache_deletion_failure_is_recorded_not_raised(self):
        self.mock_genai.caches.delete.side_effect = RuntimeError("cache already expired")

        calls, cache_metrics = run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        self.assertTrue(all(c.success for c in calls))
        self.assertFalse(cache_metrics.delete_success)
        self.assertIn("cache already expired", cache_metrics.error_message)

    def test_document_load_failure_skips_all_agents_without_touching_cache_api(self):
        missing_path = Path("dc_data/does_not_exist.png")

        calls, cache_metrics = run_approach_c(missing_path, 0, self.config, AGENTS, self.client)

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(not c.success for c in calls))
        self.mock_genai.caches.create.assert_not_called()
        self.mock_genai.caches.delete.assert_not_called()
        self.assertFalse(cache_metrics.create_success)

    def test_cached_content_token_count_only_recorded_when_api_confirms_it(self):
        self.mock_genai.models.generate_content.return_value = make_response(
            usage=make_usage(cached_tokens=1149)
        )

        calls, _ = run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        self.assertTrue(all(c.cached_content_token_count == 1149 for c in calls))

    def test_cached_content_token_count_is_none_when_api_does_not_report_it(self):
        self.mock_genai.models.generate_content.return_value = make_response(
            usage=make_usage(cached_tokens=None)
        )

        calls, _ = run_approach_c(DOCUMENT_PATH, 0, self.config, AGENTS, self.client)

        self.assertTrue(all(c.cached_content_token_count is None for c in calls))


if __name__ == "__main__":
    unittest.main()
