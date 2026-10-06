"""Optional upstream SDK 1.51 loader contracts; no conversation/model invocation.

Run with Python containing openhands-sdk==1.51.0. This suite deliberately fails
if that dependency is missing rather than silently calling a fixture a live test.
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openhands.sdk.plugin import (
    Plugin,
    disable_plugin,
    enable_plugin,
    install_plugin,
    load_installed_plugins,
)

PLUGIN = Path(__file__).resolve().parents[2]


class LoaderTests(unittest.TestCase):
    def test_native_loader_precedence_and_all_roles(self) -> None:
        self.assertEqual(importlib.metadata.version("openhands-sdk"), "1.51.0")
        loaded = Plugin.load(PLUGIN)
        self.assertEqual(loaded.entry_slash_command, "/crew:review")
        self.assertEqual(len(loaded.agents), 8)
        self.assertEqual(len(loaded.commands), 6)
        self.assertEqual(len(loaded.get_all_skills()), 6)
        self.assertIsNone(loaded.hooks)
        self.assertFalse(loaded.mcp_config)
        for agent in loaded.agents:
            self.assertEqual(agent.model, "inherit")
            self.assertIsNone(agent.permission_mode)
            self.assertEqual(agent.tools, ["terminal", "file_editor"])
            original = (
                PLUGIN
                / "agents"
                / f"{agent.name.removeprefix('crew-').rsplit('-', 1)[0]}.md"
            )
            self.assertIn(
                original.read_text().split("---\n", 2)[2].strip(), agent.system_prompt
            )

    def test_install_disable_reenable_and_self_contained_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exported = root / "exported"
            subprocess.run(
                [
                    sys.executable,
                    str(PLUGIN / "scripts/openhands-package.py"),
                    "--output",
                    str(exported),
                ],
                check=True,
            )
            self.assertEqual(
                {p.name for p in (exported / "docs").iterdir()},
                {
                    "openhands-transport.md",
                    "build-protocol.md",
                    "measure-twice-protocol.md",
                    "codex-transport.md",
                    "cursor-host.md",
                },
            )
            self.assertFalse((exported / "docs/openhands-canvas-evidence.md").exists())
            self.assertFalse(
                (exported / "docs/openhands-execution-addendum.md").exists()
            )
            installed = root / "installed"
            info = install_plugin(source=str(exported), installed_dir=installed)
            self.assertEqual(info.name, "crew")
            self.assertEqual(
                [p.name for p in load_installed_plugins(installed_dir=installed)],
                ["crew"],
            )
            self.assertTrue(disable_plugin("crew", installed_dir=installed))
            self.assertFalse(load_installed_plugins(installed_dir=installed))
            self.assertTrue(enable_plugin("crew", installed_dir=installed))
            plugin = load_installed_plugins(installed_dir=installed)[0]
            self.assertEqual(len(plugin.agents), 8)
            package = Path(plugin.path)
            self.assertFalse(any(path.is_symlink() for path in package.rglob("*")))
            subprocess.run(
                [
                    sys.executable,
                    str(package / "scripts/multiagent/cli.py"),
                    "project-root",
                ],
                cwd=temporary,
                check=True,
                capture_output=True,
            )

    def test_content_names_avoid_old_first_wins_registration(self) -> None:
        from openhands.sdk.subagent.registry import (
            _reset_registry_for_tests,
            get_agent_factory,
            register_agent,
            register_plugin_agents,
        )
        from openhands.sdk.subagent.schema import AgentDefinition

        _reset_registry_for_tests()
        self.addCleanup(_reset_registry_for_tests)
        legacy = AgentDefinition(name="crew-reviewer", description="stale legacy role")
        register_agent(legacy.name, lambda llm: None, legacy)
        plugin = Plugin.load(PLUGIN)
        registered = register_plugin_agents(plugin.agents)
        self.assertEqual(set(registered), {agent.name for agent in plugin.agents})
        for agent in plugin.agents:
            self.assertEqual(get_agent_factory(agent.name).definition, agent)
        self.assertEqual(register_plugin_agents(plugin.agents), [])
        self.assertEqual(get_agent_factory("crew-reviewer").definition, legacy)
        with self.assertRaises(ValueError):
            get_agent_factory("crew-reviewer-unknown-content")

    def test_changed_role_content_gets_a_distinct_sdk_registration(self) -> None:
        import shutil

        from openhands.sdk.subagent.registry import (
            _reset_registry_for_tests,
            get_agent_factory,
            register_plugin_agents,
        )

        _reset_registry_for_tests()
        self.addCleanup(_reset_registry_for_tests)
        original = Plugin.load(PLUGIN)
        register_plugin_agents(original.agents)
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            for name in ("agents", "dev.openhands", "scripts"):
                shutil.copytree(
                    PLUGIN / name,
                    package / name,
                    ignore=shutil.ignore_patterns("__pycache__", "tests"),
                )
            shutil.copy2(PLUGIN / "plugin.json", package / "plugin.json")
            path = package / "agents/reviewer.md"
            path.write_text(path.read_text() + "\nReview only this exact target.\n")
            subprocess.run(
                [sys.executable, str(package / "scripts/openhands-package.py")],
                check=True,
            )
            updated = Plugin.load(package)
            names = register_plugin_agents(updated.agents)
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].startswith("crew-reviewer-"))
            self.assertIn(
                "Review only this exact target.",
                get_agent_factory(names[0]).definition.system_prompt,
            )

    def test_exact_name_collision_is_an_explicit_host_trust_limit(self) -> None:
        from openhands.sdk.subagent.registry import (
            _reset_registry_for_tests,
            get_agent_factory,
            register_agent,
            register_plugin_agents,
        )
        from openhands.sdk.subagent.schema import AgentDefinition

        _reset_registry_for_tests()
        self.addCleanup(_reset_registry_for_tests)
        actual = Plugin.load(PLUGIN).agents[0]
        impostor = AgentDefinition(
            name=actual.name, description="explicitly shadowed name"
        )
        register_agent(impostor.name, lambda llm: None, impostor)
        self.assertEqual(register_plugin_agents([actual]), [])
        self.assertNotEqual(get_agent_factory(actual.name).definition, actual)

    def test_effective_connection_routing_and_supported_secret_stability(self) -> None:
        from openhands.sdk import LLM
        from openhands.sdk.llm.llm import LLM_SECRET_FIELDS
        from openhands.sdk.llm.llm_profile_store import LLMProfileStore
        from openhands.sdk.llm.provider_connection_store import (
            ProviderConnection,
            ProviderConnectionStore,
        )
        from pydantic import SecretStr

        sys.path.insert(0, str(PLUGIN / "scripts"))
        from multiagent import openhands_native_transport as native

        self.assertEqual(
            native._nonsecret_settings({key: "encrypted" for key in LLM_SECRET_FIELDS}),
            {},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connections = ProviderConnectionStore(root / "provider-connections")
            connection = ProviderConnection(
                id="shared",
                display_name="Fixture",
                base_url="https://first.invalid",
                api_key=SecretStr("fixture-secret"),
                created_at=1,
                updated_at=1,
            )
            connections.create(connection)
            store = LLMProfileStore(root / "profiles")
            llm = LLM(
                model="openai/fixture",
                provider_connection_id="shared",
                aws_access_key_id=SecretStr("fixture-id"),
                aws_session_token=SecretStr("fixture-token"),
            )
            store.save("fixture", llm, include_secrets=True)
            path = root / "profiles/fixture.json"
            before = native._profile_fingerprint(path)
            original = path.read_bytes()
            self.assertEqual(store.load("fixture").base_url, "https://first.invalid")
            store.save("fixture", llm, include_secrets=True)
            connections.update(connection.model_copy(update={"updated_at": 2}))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(native._profile_fingerprint(path), before)
            connections.update(
                connection.model_copy(update={"base_url": "https://second.invalid"}),
            )
            self.assertEqual(store.load("fixture").base_url, "https://second.invalid")
            self.assertNotEqual(native._profile_fingerprint(path), before)

    def factory_agent(self, definition: object, parent: object) -> object:
        from openhands.sdk.subagent.registry import agent_definition_to_factory
        from openhands.sdk.tool import registry
        from openhands.sdk.tool.builtins import FinishTool

        # Real registry/factory, disposable tool implementations; no tool execution.
        with (
            mock.patch.dict(registry._REG, clear=True),
            mock.patch.dict(registry._TOOL_CLASSES, clear=True),
            mock.patch.dict(registry._USABILITY_REG, clear=True),
        ):
            for name in definition.tools:
                registry.register_tool(name, FinishTool)
            return agent_definition_to_factory(definition)(parent)

    def test_factory_credentials_without_cipher_and_inherited_route(self) -> None:
        from openhands.sdk import LLM
        from openhands.sdk.llm.llm import LLM_SECRET_FIELDS
        from openhands.sdk.llm.llm_profile_store import LLMProfileStore
        from openhands.sdk.llm.provider_connection_store import (
            ProviderConnection,
            ProviderConnectionStore,
        )
        from openhands.sdk.utils.cipher import FERNET_TOKEN_PREFIX, Cipher
        from pydantic import SecretStr

        sys.path.insert(0, str(PLUGIN / "scripts"))
        from multiagent import openhands_native_transport as native
        from multiagent import review_workflow as review

        self.assertEqual(native.LLM_SECRET_FIELDS, LLM_SECRET_FIELDS)
        self.assertEqual(native.FERNET_TOKEN_PREFIX, FERNET_TOKEN_PREFIX)
        parent = LLM(model="openai/fixture", api_key=SecretStr("disposable-parent"))
        inherited = Plugin.load(PLUGIN).agents[0]
        self.assertIs(self.factory_agent(inherited, parent).llm, parent)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exported, profiles = root / "exported", root / "profiles"
            subprocess.run(
                [
                    sys.executable,
                    str(PLUGIN / "scripts/openhands-package.py"),
                    "--output",
                    str(exported),
                    "--profile",
                    "fixture",
                    "--profile-store-dir",
                    str(profiles),
                ],
                check=True,
            )
            definition = next(
                agent
                for agent in Plugin.load(exported).agents
                if agent.model == "fixture"
            )
            store = LLMProfileStore(profiles)
            cipher = Cipher("disposable-encryption-key")
            for field in LLM_SECRET_FIELDS:
                with self.subTest(field=field):
                    llm = LLM(
                        model="openai/fixture",
                        **{field: SecretStr("disposable-credential")},
                    )
                    store.save("fixture", llm, include_secrets=True, cipher=cipher)
                    produced = self.factory_agent(definition, parent)
                    self.assertTrue(
                        getattr(produced.llm, field)
                        .get_secret_value()
                        .startswith(FERNET_TOKEN_PREFIX)
                    )
                    with self.assertRaises(review.WorkflowError) as error:
                        native._profile_fingerprint(profiles / "fixture.json")
                    self.assertEqual(error.exception.code, "encrypted_native_profile")
            connections = ProviderConnectionStore(root / "provider-connections")
            connection = ProviderConnection(
                id="shared",
                display_name="Fixture",
                api_key=SecretStr("disposable-credential"),
                created_at=1,
                updated_at=1,
            )
            connections.create(connection, cipher=cipher)
            store.save(
                "fixture", LLM(model="openai/fixture", provider_connection_id="shared")
            )
            produced = self.factory_agent(definition, parent)
            self.assertTrue(
                produced.llm.api_key.get_secret_value().startswith(FERNET_TOKEN_PREFIX)
            )
            with self.assertRaises(review.WorkflowError) as error:
                native._profile_fingerprint(profiles / "fixture.json")
            self.assertEqual(error.exception.code, "encrypted_native_profile")
            # Existing native profiles without encrypted credentials remain supported.
            store.save("fixture", LLM(model="openai/fixture"))
            self.assertEqual(
                self.factory_agent(definition, parent).llm.model, "openai/fixture"
            )
            native._profile_fingerprint(profiles / "fixture.json")
            store.save(
                "fixture",
                LLM(
                    model="openai/fixture",
                    api_key=SecretStr("disposable-existing-profile"),
                ),
                include_secrets=True,
            )
            self.assertTrue(
                self.factory_agent(definition, parent).llm.api_key.get_secret_value()
                == "disposable-existing-profile"
            )
            native._profile_fingerprint(profiles / "fixture.json")
            connections.update(connection)
            store.save(
                "fixture", LLM(model="openai/fixture", provider_connection_id="shared")
            )
            self.assertTrue(
                self.factory_agent(definition, parent).llm.api_key.get_secret_value()
                == "disposable-credential"
            )
            native._profile_fingerprint(profiles / "fixture.json")

    def test_sdk_tool_serialization_registration_and_admission(self) -> None:
        from openhands.sdk import LLM, Agent
        from openhands.sdk.tool import Tool, registry
        from openhands.sdk.tool.builtins import FinishTool
        from openhands.sdk.tool.defaults import SUB_AGENT_TOOL_NAME, canonical_tool_name

        sys.path.insert(0, str(PLUGIN / "scripts"))
        from multiagent import openhands_native_transport as native

        self.assertEqual(SUB_AGENT_TOOL_NAME, "task_tool_set")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "events").mkdir()
            for names in (
                ("terminal", "file_editor", "task_tool_set"),
                ("TerminalTool", "FileEditorTool", "TaskToolSet"),
            ):
                specs = [Tool(name=name, params={"fixture": True}) for name in names]
                agent = Agent(llm=LLM(model="openai/fixture"), tools=specs)
                state = {
                    "id": "fixture",
                    "workspace": {"working_dir": str(root)},
                    "agent": agent.model_dump(mode="json"),
                }
                (root / "base_state.json").write_text(json.dumps(state))
                host = native.inspect_context("fixture-backend", root, root)
                self.assertEqual(host.tools, tuple(sorted(names)))
                for spec in specs:
                    self.assertEqual(canonical_tool_name(spec.name), spec.name)
                with (
                    mock.patch.dict(registry._REG, clear=True),
                    mock.patch.dict(registry._TOOL_CLASSES, clear=True),
                    mock.patch.dict(registry._USABILITY_REG, clear=True),
                ):
                    for name in names:
                        registry.register_tool(name, FinishTool)
                    self.assertEqual(set(registry.list_registered_tools()), set(names))
                    for name in names:
                        self.assertIs(registry.registered_tool_class(name), FinishTool)

    def test_named_profiles_are_references_and_load_as_native_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exported = root / "exported"
            profiles = root / "existing-host-profiles"
            subprocess.run(
                [
                    sys.executable,
                    str(PLUGIN / "scripts/openhands-package.py"),
                    "--output",
                    str(exported),
                    "--profile",
                    "configured-a",
                    "--profile",
                    "configured-b",
                    "--profile-store-dir",
                    str(profiles),
                ],
                check=True,
            )
            plugin = Plugin.load(exported)
            self.assertEqual(len(plugin.agents), 24)
            self.assertEqual(
                {a.model for a in plugin.agents},
                {"inherit", "configured-a", "configured-b"},
            )
            for agent in plugin.agents:
                if agent.model != "inherit":
                    self.assertEqual(agent.profile_store_dir, str(profiles.resolve()))
            routes = json.loads((exported / "dev.openhands/routes.json").read_text())
            self.assertEqual(set(routes), {"inherit", "configured-a", "configured-b"})
            self.assertFalse(profiles.exists())


if __name__ == "__main__":
    unittest.main()
