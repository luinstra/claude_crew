"""Small test-only adapter for the standalone review protocol.

The fake deliberately uses only the seven public workflow callables. Tests can
drive deterministic host behavior without reaching into workflow internals or
introducing a production adapter registry.
"""

from __future__ import annotations

import json
from pathlib import Path

from multiagent import review_workflow


class InMemoryReviewDriver:
    """Typed test driver over the public standalone workflow interface."""

    def __init__(self, session_id: str = "test-session") -> None:
        self.session_id = session_id
        self.last: review_workflow.ReviewStep | None = None

    def start(self, target: str, **kwargs: object) -> review_workflow.ReviewStep:
        self.last = review_workflow.start_review(
            review_workflow.ReviewRequest(
                target,
                session_id=self.session_id,
                **kwargs,
            )
        )
        return self.last

    def next(self, ref: review_workflow.ReviewRef) -> review_workflow.ReviewStep:
        self.last = review_workflow.next_review(ref)
        return self.last

    def claim(
        self,
        ref: review_workflow.ReviewRef,
        action_id: str,
    ) -> review_workflow.ClaimResponse:
        return review_workflow.claim_review_action(
            review_workflow.ClaimRequest(ref, action_id)
        )

    def execute(
        self,
        ref: review_workflow.ReviewRef,
        action_id: str,
    ) -> review_workflow.ReviewStep:
        self.last = review_workflow.execute_external_review(ref, action_id)
        return self.last

    def submit(
        self,
        path: str,
        *,
        consume: bool = True,
    ) -> review_workflow.ReviewStep:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        result = review_workflow.parse_host_result(payload)
        self.last = review_workflow.submit_review(
            review_workflow.SubmissionRequest(path, consume, result)
        )
        return self.last

    def recover(
        self,
        ref: review_workflow.ReviewRef,
        action_id: str,
        diagnostic_code: str,
    ) -> review_workflow.ReviewStep:
        self.last = review_workflow.recover_review_action(
            review_workflow.RecoveryRequest(
                ref,
                action_id,
                "not_running",
                diagnostic_code,
            )
        )
        return self.last

    def retry(
        self,
        ref: review_workflow.ReviewRef,
        seats: tuple[str, ...] | None = None,
    ) -> review_workflow.ReviewStep:
        self.last = review_workflow.retry_review(
            review_workflow.RetryRequest(ref, seats)
        )
        return self.last
