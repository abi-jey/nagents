from __future__ import annotations

import unittest

from nagents.events import ErrorEvent
from nagents.events import RateLimitEvent

from ngn_nagents.errors import provider_error_text
from ngn_nagents.errors import retry_notice_text


class ProviderErrorTextTests(unittest.TestCase):
    def test_http_statuses_get_specific_actionable_explanations(self) -> None:
        expectations = {
            "401": "credentials",
            "CODEX_HTTP_401": "credentials",
            "403": "denied access",
            "404": "model ID",
            "MODEL_NOT_FOUND": "model ID",
            "429": "rate limit",
            "CODEX_HTTP_429": "rate limit",
            "400": "rejected the request",
            "500": "server error",
            "503": "server error",
        }
        for code, phrase in expectations.items():
            with self.subTest(code=code):
                text = provider_error_text(ErrorEvent(message="secret upstream body", code=code))
                self.assertIn(phrase, text)
                self.assertNotIn("secret upstream body", text, "Upstream payloads must never be echoed")

    def test_status_is_shown_but_arbitrary_codes_are_not(self) -> None:
        self.assertIn("HTTP 429", provider_error_text(ErrorEvent(code="429")))
        for code in ("<script>", "x" * 500, None, "PLUGIN_SECRET_42"):
            with self.subTest(code=code):
                text = provider_error_text(ErrorEvent(message="raw", code=code))
                self.assertEqual(
                    text,
                    "The provider request failed. Check the provider, model and credentials, then send again.",
                )

    def test_transport_failures_use_fixed_categories(self) -> None:
        event = ErrorEvent(
            code="PROVIDER_REQUEST_FAILED",
            extra={"transport": {"category": "timeout", "phase": "response"}},
        )
        self.assertIn("timed out", provider_error_text(event))
        event = ErrorEvent(
            code="PROVIDER_REQUEST_FAILED",
            extra={"transport": {"category": "dns", "phase": "request"}},
        )
        self.assertIn("could not be resolved", provider_error_text(event))
        event = ErrorEvent(code="PROVIDER_REQUEST_FAILED", extra={"transport": {"category": "<raw>", "phase": "x"}})
        self.assertNotIn("<raw>", provider_error_text(event))


class RetryNoticeTests(unittest.TestCase):
    def test_retry_notice_names_status_delay_and_attempt(self) -> None:
        text = retry_notice_text(RateLimitEvent(attempt=1, max_retries=3, retry_after=5.0, status_code=429))
        self.assertEqual(text, "Provider rate limit (HTTP 429). Retrying in 5s (attempt 1 of 3)…")
        text = retry_notice_text(RateLimitEvent(attempt=2, max_retries=3, retry_after=10.4, status_code=503))
        self.assertEqual(text, "Provider server error (HTTP 503). Retrying in 10s (attempt 2 of 3)…")

    def test_retry_notice_bounds_untrusted_numbers(self) -> None:
        text = retry_notice_text(
            RateLimitEvent(attempt=-4, max_retries=10**9, retry_after=float("nan"), status_code=99999)
        )
        self.assertEqual(text, "Provider request failed. Retrying shortly…")


if __name__ == "__main__":
    unittest.main()
