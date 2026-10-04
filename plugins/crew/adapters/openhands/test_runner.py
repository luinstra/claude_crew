"""Exercise the user entry point without provider credentials or network access."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_sdk_contract import LLM, MODELS, fixture_response

# isort: split
from runner import load_settings, main
from test_bridge import config


class RunnerTests(unittest.TestCase):
    def test_explicit_credentials_and_model_routes_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.toml"
            path.write_text(
                'max_concurrency = 2\nsupport_model = "provider/model"\n[models."provider/model"]\napi_key_env = "CREW_TEST_MISSING_KEY"\n'
            )
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                load_settings(path)
            with patch.dict(os.environ, {"CREW_TEST_MISSING_KEY": "offline"}):
                self.assertEqual(load_settings(path)["support_model"], "provider/model")

    def test_entrypoint_review_and_explicit_resume(self) -> None:
        calls = []

        async def complete(llm: LLM, **kwargs: object):
            calls.append(llm.model)
            text = str(kwargs.get("messages"))
            report = (
                'Summary\nCREW_JUDGMENT: {"verdict":"APPROVED","minor_only":false}'
                if "CREW_JUDGMENT" in text
                else "## VERDICT\nAPPROVED\n\n## FINDINGS\nnone\n"
            )
            return fixture_response("finish", {"message": report})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            plan = root / "plan.md"
            plan.write_text("# Test plan\nReview a harmless greeting.")
            settings = root / "runtime.toml"
            settings.write_text(
                f'max_concurrency = 2\nsupport_model = "{MODELS[0]}"\n[models."{MODELS[0]}"]\napi_key_env = "CREW_TEST_KEY"\nmax_input_tokens = 32768\nmax_output_tokens = 1024\n'
            )
            config._reset_cache_for_tests()
            config._global_cache = {}
            config._cache = {
                "seats": {"oh-one": {"via": ["openhands"], "model": MODELS[0]}}
            }
            args = [
                "--workspace",
                str(root),
                "--config",
                str(settings),
                "--session-id",
                "oh-entrypoint",
            ]
            try:
                with (
                    patch.dict(os.environ, {"CREW_TEST_KEY": "offline"}),
                    contextlib.chdir(root),
                    patch.object(LLM, "agenerate", complete),
                ):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        status = main([*args, "review", str(plan), "--seats", "oh-one"])
                    result = json.loads(output.getvalue())
                    self.assertEqual(status, 0)
                    self.assertEqual(result["outcome"]["judgment"], "APPROVED")
                    ref = root / "ref.json"
                    ref.write_text(json.dumps(result["ref"]))
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        status = main(
                            [*args, "resume", "--kind", "review", "--ref", str(ref)]
                        )
                    self.assertEqual(status, 0)
                    self.assertEqual(
                        json.loads(output.getvalue())["outcome"], result["outcome"]
                    )
                    self.assertEqual(len(calls), 2)
            finally:
                config._reset_cache_for_tests()


if __name__ == "__main__":
    unittest.main()
