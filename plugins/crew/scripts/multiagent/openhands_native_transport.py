"""OpenHands task transport: native invocations and persisted SDK event capture.

No model execution occurs here. The native parent calls its task tool; this
module claims engine work, records launch intent and reads host event files.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None

from multiagent import build_workflow as build
from multiagent import channels, seats
from multiagent import measure_twice as measure
from multiagent import review_workflow as review

Ref = review.ReviewRef | measure.MeasureRef | build.BuildRef
PLUGIN = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class HostContext:
    backend_id: str
    conversation_id: str
    conversation_dir: str
    workspace: str
    model: str
    tools: tuple[str, ...]
    routes: dict[str, Any]
    profile_hashes: dict[str, str]
    role_hashes: dict[str, str]
    effective_sha256: str

    @property
    def session_id(self) -> str:
        identity = [self.backend_id, self.conversation_id, self.workspace]
        return "openhands-" + measure.sha256(measure._canonical(identity))[:32]


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise review.WorkflowError(
            "invalid_host_evidence", f"expected regular file: {path}"
        )
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise review.WorkflowError(
            "invalid_host_evidence", f"cannot read {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise review.WorkflowError(
            "invalid_host_evidence", f"expected JSON object: {path}"
        )
    return value


def _nonsecret_settings(value: object) -> object:
    """Fingerprint effective settings without retaining credentials or endpoint text."""
    if isinstance(value, dict):
        return {
            key: _nonsecret_settings(item)
            for key, item in value.items()
            if not any(
                secret in key.lower()
                for secret in (
                    "api_key",
                    "aws_access_key_id",
                    "aws_session_token",
                    "password",
                    "secret",
                    "access_token",
                    "auth_token",
                    "credential",
                    "authorization",
                )
            )
        }
    if isinstance(value, list):
        return [_nonsecret_settings(item) for item in value]
    return value


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise review.WorkflowError(
            "invalid_host_evidence", f"{label} must be an object"
        )
    return value


def _list(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise review.WorkflowError("invalid_host_evidence", f"{label} must be a list")
    return value


def parse_context(data: dict) -> HostContext:
    names = {field.name for field in dataclasses.fields(HostContext)}
    if set(data) != names:
        raise review.WorkflowError("invalid_host_evidence", "context fields differ")
    for key in names - {"tools", "routes", "profile_hashes", "role_hashes"}:
        if not isinstance(data[key], str) or not data[key]:
            raise review.WorkflowError(
                "invalid_host_evidence", f"context {key} must be nonempty text"
            )
    if any(
        not isinstance(tool, str) or not tool for tool in _list(data["tools"], "tools")
    ):
        raise review.WorkflowError(
            "invalid_host_evidence", "context tools must be names"
        )
    for key in ("routes", "profile_hashes", "role_hashes"):
        _object(data[key], key)
    for route in data["routes"].values():
        roles = _object(_object(route, "profile route").get("roles"), "route roles")
        if any(not isinstance(name, str) or not name for name in roles.values()):
            raise review.WorkflowError(
                "invalid_host_evidence", "role names must be text"
            )
    return HostContext(**{**data, "tools": tuple(data["tools"])})


# SDK 1.51.0 secret serializer fields and Fernet discriminator.
LLM_SECRET_FIELDS = (
    "api_key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
)
FERNET_TOKEN_PREFIX = "gAAAAA"


def _require_factory_credentials(data: dict, fields: tuple[str, ...]) -> None:
    for field in fields:
        value = data.get(field)
        if isinstance(value, str) and value.startswith(FERNET_TOKEN_PREFIX):
            raise review.WorkflowError(
                "encrypted_native_profile",
                "SDK 1.51.0 native named-agent factory loads profiles without a cipher; "
                "encrypted profile or linked provider credentials are unsupported. "
                "Use the inherited parent route or a host-supported native profile; "
                "do not copy credentials to plaintext.",
            )


def _profile_fingerprint(path: Path) -> str:
    data = read_json(path)
    _require_factory_credentials(data, LLM_SECRET_FIELDS)
    if not isinstance(data.get("model"), str) or not data["model"]:
        raise review.WorkflowError("missing_profile", "named profile has no model")
    connection_id = data.get("provider_connection_id")
    if connection_id is not None:
        if not isinstance(connection_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", connection_id
        ):
            raise review.WorkflowError(
                "invalid_host_evidence", "invalid provider connection reference"
            )
        # Native named agents use LLMProfileStore's default sibling connection store.
        store = read_json(
            path.parent.parent / "provider-connections/provider_connections.json"
        )
        version = store.get("schema_version", 1)
        if type(version) is not int or version != 1:
            raise review.WorkflowError(
                "invalid_host_evidence", "unsupported provider connection schema"
            )
        connections = _list(store.get("connections", []), "provider connections")
        matched = []
        identities = set()
        for value in connections:
            connection = _object(value, "provider connection")
            identity = connection.get("id")
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identity)
                or identity in identities
            ):
                raise review.WorkflowError(
                    "invalid_host_evidence",
                    "invalid or duplicate provider connection id",
                )
            identities.add(identity)
            for key in ("display_name", "provider"):
                name = connection.get(key, "custom" if key == "provider" else None)
                if not isinstance(name, str) or not 1 <= len(name) <= 128:
                    raise review.WorkflowError(
                        "invalid_host_evidence", f"invalid connection {key}"
                    )
            for key in ("created_at", "updated_at"):
                if type(connection.get(key)) is not int:
                    raise review.WorkflowError(
                        "invalid_host_evidence", f"invalid connection {key}"
                    )
            endpoint = connection.get("base_url")
            if endpoint is not None and (
                not isinstance(endpoint, str) or len(endpoint) > 2048
            ):
                raise review.WorkflowError(
                    "invalid_host_evidence", "invalid provider endpoint"
                )
            if connection.get("api_key") is not None and not isinstance(
                connection["api_key"], str
            ):
                raise review.WorkflowError(
                    "invalid_host_evidence", "invalid provider credential shape"
                )
            if identity == connection_id:
                matched.append(connection)
        if len(matched) != 1:
            raise review.WorkflowError(
                "missing_profile", "referenced provider connection is missing"
            )
        _require_factory_credentials(matched[0], ("api_key",))
        # The connection endpoint wins even when null; timestamps and credentials are not routing.
        data = {**data, "base_url": matched[0].get("base_url")}
    return measure.sha256(measure._canonical(_nonsecret_settings(data)))


def inspect_context(
    backend_id: str, conversation_dir: Path, workspace: Path
) -> HostContext:
    """Read only host configuration, retaining no credentials or endpoints."""
    if not backend_id.strip():
        raise review.WorkflowError(
            "missing_backend", "provide the active backend identity"
        )
    state = read_json(conversation_dir / "base_state.json")
    working_dir = _object(state.get("workspace"), "workspace").get("working_dir")
    if (
        not isinstance(working_dir, str)
        or Path(working_dir).resolve() != workspace.resolve()
    ):
        raise review.WorkflowError(
            "workspace_mismatch",
            "the persisted native workspace must match the tool workspace",
        )
    agent = _object(state.get("agent"), "agent")
    llm = _object(agent.get("llm", {}), "agent llm")
    if agent.get("kind") != "Agent":
        raise review.WorkflowError(
            "unsupported_agent",
            "Crew requires a native OpenHands Agent, not an ACP agent",
        )
    tool_names = []
    for value in _list(agent.get("tools", []), "agent tools"):
        name = _object(value, "tool").get("name")
        if not isinstance(name, str) or not name:
            raise review.WorkflowError(
                "invalid_host_evidence", "tool name must be nonempty text"
            )
        tool_names.append(name)
    tools = tuple(sorted(tool_names))
    if not {"task_tool_set", "TaskToolSet"}.intersection(tools):
        raise review.WorkflowError(
            "missing_task_tool",
            "enable TaskToolSet in the existing native agent configuration",
        )
    if not {"terminal", "TerminalTool"}.intersection(tools) or not {
        "file_editor",
        "FileEditorTool",
    }.intersection(tools):
        raise review.WorkflowError(
            "missing_tools",
            "Crew roles require the parent's TerminalTool and FileEditorTool",
        )
    model = llm.get("model")
    conversation_id = state.get("id")
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(conversation_id, str)
        or not conversation_id
    ):
        raise review.WorkflowError(
            "invalid_host_evidence",
            "conversation id and configured parent model are required",
        )
    events = conversation_dir / "events"
    if events.is_symlink() or not events.is_dir():
        raise review.WorkflowError(
            "missing_event_store",
            "the actual backend event directory must be locally readable",
        )
    routes = read_json(PLUGIN / "dev.openhands" / "routes.json")
    hashes = {}
    role_hashes = {}
    for profile, route in routes.items():
        route = _object(route, "profile route")
        roles = _object(route.get("roles"), "route roles")
        for role_name in roles.values():
            if not isinstance(role_name, str) or not review.SESSION_RE.fullmatch(
                role_name
            ):
                raise review.WorkflowError(
                    "unresolved_native_role", "invalid packaged native role name"
                )
            role_path = PLUGIN / "dev.openhands" / "agents" / f"{role_name}.md"
            if role_path.is_symlink() or not role_path.is_file():
                raise review.WorkflowError(
                    "unresolved_native_role", "packaged native role is missing"
                )
            role_bytes = role_path.read_bytes()
            normalized = role_bytes.replace(
                f"name: {role_name}\n".encode(), b"name: __CREW_ROLE__\n", 1
            )
            if role_name.rsplit("-", 1)[-1] != measure.sha256(normalized):
                raise review.WorkflowError(
                    "unresolved_native_role",
                    "packaged role content does not match its registered content name",
                )
            role_hashes[role_name] = measure.sha256(role_bytes)
        if profile != "inherit":
            if (
                not isinstance(route.get("profile_path"), str)
                or not route["profile_path"]
            ):
                raise review.WorkflowError(
                    "invalid_host_evidence", "named profile path must be text"
                )
            hashes[profile] = _profile_fingerprint(Path(route["profile_path"]))
    return HostContext(
        backend_id,
        conversation_id,
        str(conversation_dir.resolve()),
        str(workspace.resolve()),
        model,
        tools,
        routes,
        hashes,
        role_hashes,
        measure.sha256(
            measure._canonical(
                _nonsecret_settings(
                    {
                        "agent": agent,
                        "confirmation_policy": state.get("confirmation_policy"),
                        "security_analyzer": state.get("security_analyzer"),
                    }
                )
            )
        ),
    )


def context(*, validate: bool = True) -> HostContext:
    path = os.environ.get("CREW_OPENHANDS_CONTEXT")
    if not path:
        raise review.WorkflowError(
            "missing_host_context",
            "invoke Crew through scripts/openhands.py run inside the native conversation",
        )
    data = read_json(Path(path))
    saved = parse_context(data)
    current = (
        inspect_context(
            saved.backend_id, Path(saved.conversation_dir), Path(saved.workspace)
        )
        if validate
        else saved
    )
    if current != saved:
        raise review.WorkflowError(
            "host_configuration_changed",
            "native agent, profiles or plugin routes changed; restore the frozen configuration before resume",
        )
    from state_discovery import crew_base

    if str(crew_base().resolve()) != saved.workspace:
        raise review.WorkflowError(
            "workspace_mismatch", "Crew project root differs from the native workspace"
        )
    return saved


def select_context(host: HostContext, *, new_generation: bool = False) -> Path:
    """Keep immutable generations; an explicit new-work transition changes the pointer."""
    from models import atomic_write_json
    from state_discovery import crew_base

    with _transport_lock():
        root = crew_base() / ".crew" / "openhands"
        body = measure._canonical(dataclasses.asdict(host))
        snapshot = measure._safe_path(
            root / "contexts" / f"{host.session_id}-{measure.sha256(body)}.json",
            crew_base(),
        )
        pointer = measure._safe_path(root / f"{host.session_id}.json", crew_base())
        if pointer.exists() and read_json(pointer) != json.loads(body):
            if not new_generation:
                raise review.WorkflowError(
                    "host_configuration_changed",
                    "use --new-context for new work; recover old work using its --context-file",
                )
            if _busy(host.session_id):
                raise review.WorkflowError(
                    "native_busy",
                    "retire old invocations before selecting a new context generation",
                )
        measure._write_once(snapshot, body, crew_base())
        atomic_write_json(pointer, json.loads(body))
        return snapshot


def resolve_roster(request: object, policy: review.RoutePolicy) -> list[tuple]:
    host = context()
    if request.panel is not None:
        raise review.WorkflowError(
            "unsupported_panel",
            "OpenHands uses explicit native --seats; omit --panel for one inherited seat",
        )
    names = request.seats.split(",") if request.seats else ["openhands-inherit"]
    answer = []
    profiles = set()
    for name in names:
        name = name.strip()
        spec = (
            seats.SeatSpec(name, ("openhands",), model="inherit")
            if name == "openhands-inherit"
            else seats.seat_spec(name)
        )
        if spec is None or spec.via != ("openhands",) or spec.model not in host.routes:
            raise review.WorkflowError(
                "unsupported_native_profile",
                f"seat {name!r} must use only openhands and a packaged existing profile",
            )
        if spec.reasoning_effort is not None:
            raise review.WorkflowError(
                "unsupported_native_effort",
                "configure reasoning in the existing OpenHands profile",
            )
        if spec.model in profiles:
            raise review.WorkflowError(
                "duplicate_profile", "each OpenHands profile may vote only once"
            )
        profiles.add(spec.model)
        execution = channels.resolve_seat(spec, declared_native="openhands")
        if (
            not execution
            or not execution.native
            or "openhands" in policy.force_external
        ):
            raise review.WorkflowError(
                "route_unavailable",
                "OpenHands cannot fall back to an external provider",
            )
        answer.append((name, spec, execution))
    return answer


def context_path(ref: Ref) -> Path:
    # Review attempts share one admission binding; action/ref artifacts stay separate.
    owner = (
        dataclasses.replace(ref, attempt_id="attempt-0001")
        if isinstance(ref, review.ReviewRef)
        else ref
    )
    return _root(owner) / "context.json"


def bound_context(ref: Ref) -> HostContext:
    binding = context_path(ref)
    if not binding.is_file():
        raise review.WorkflowError(
            "missing_host_context", f"owner has no admitted context: {binding}"
        )
    host = parse_context(read_json(binding))
    if ref.session_segment != host.session_id:
        raise review.WorkflowError(
            "session_mismatch", "owner context belongs to another backend/conversation"
        )
    return host


def validate_context(ref: Ref) -> HostContext:
    saved = bound_context(ref)
    supplied = context(validate=False)
    if ref.session_segment != supplied.session_id:
        raise review.WorkflowError(
            "session_mismatch", "engine owner belongs to another backend/conversation"
        )
    if saved != supplied:
        raise review.WorkflowError(
            "host_configuration_changed",
            "this owner retains its original context generation",
        )
    return context()


def freeze_context(ref: Ref, host: HostContext | None = None) -> None:
    host = host or context()
    if ref.session_segment != host.session_id:
        raise review.WorkflowError(
            "session_mismatch", "engine owner belongs to another backend/conversation"
        )
    binding = context_path(ref)
    body = measure._canonical(dataclasses.asdict(host))
    if binding.exists() and binding.read_bytes() != body:
        raise review.WorkflowError(
            "host_configuration_changed",
            "this owner retains its original context generation",
        )
    measure._write_once(binding, body, binding.parent)


def admit_review_context(
    ref: review.ReviewRef, run: Path, host: HostContext | None = None
) -> None:
    """Called under the review lock, before publishing an executable workflow."""
    host = host or context()
    expected = review._guard_review_path(
        session_segment=ref.session_segment, run_id=ref.run_id, create=False
    )
    if run != expected or ref.session_segment != host.session_id:
        raise review.WorkflowError(
            "session_mismatch", "review admission context differs"
        )
    binding = measure._safe_path(context_path(ref), _root(ref))
    # Without workflow.json no action can be claimed. Replacing this orphan remains
    # retryable across crashes; once the workflow exists, the binding is immutable.
    if not (run / "workflow.json").exists():
        review.review_runs._atomic_write_text(
            binding, measure._canonical(dataclasses.asdict(host)).decode("utf-8")
        )
    else:
        freeze_context(ref, host)


def launch_metadata(
    ref: Ref,
    action_id: str,
    role: str,
    model: str | None,
    effort: str | None,
    prompt_path: str,
    returned_path: str,
) -> dict:
    # Rendering retained work must remain possible during recovery after live drift.
    host = bound_context(ref)
    profile = model or "inherit"
    route = host.routes.get(profile, {}).get("roles", {}).get(role)
    if not route or effort is not None:
        raise review.WorkflowError(
            "unresolved_native_role",
            "role/profile is not packaged, or per-task effort was requested",
        )
    token = measure.sha256(
        measure._canonical(dataclasses.asdict(ref)) + action_id.encode()
    )
    prompt = (
        f"Crew action {token}. Read {prompt_path} and perform exactly the issued action. "
        "Return the full final report directly. Do not delegate, start another Crew workflow, "
        "or mutate engine state. Role access restrictions are advisory; preserve host confirmation policy."
    )
    return {
        "api": "openhands.task",
        "task": {"prompt": prompt, "subagent_type": route},
        "returned_path": returned_path,
        "capture": "persisted-events",
        "continuation": "fresh",
        "max_concurrency": 1,
        "model_attribution": "configured-profile-only",
        "profile": profile,
        "configured_parent_model": host.model,
        "prepare": [str(PLUGIN / "scripts" / "openhands.py"), "prepare"],
        "capture_command": [str(PLUGIN / "scripts" / "openhands.py"), "capture"],
    }


def owner_command(ref: Ref, argv: tuple[str, ...]) -> tuple[str, ...]:
    """Project owner commands through their frozen host, including parent work."""
    if not ref.session_segment.startswith("openhands-"):
        return argv
    binding = context_path(ref)
    if not binding.exists():
        return argv
    if argv[0] == str(PLUGIN / "scripts/openhands.py"):
        return argv
    return (
        str(PLUGIN / "scripts/openhands.py"),
        "run",
        "--context-file",
        str(binding),
        "--",
        *argv,
    )


def owner_commands(
    ref: measure.MeasureRef | build.BuildRef, verbs: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    if (
        not ref.session_segment.startswith("openhands-")
        or not context_path(ref).exists()
    ):
        return {}
    flags = (
        "--session-segment",
        ref.session_segment,
        "--loop-instance-id",
        ref.loop_instance_id,
    )
    commands = {}
    for verb in verbs:
        arguments = () if verb.endswith("-resume") else flags
        if verb.endswith("-cancel"):
            arguments = (*arguments, "--reason", "operator cancelled")
        # Decisions require the question-id/kind and answer from the bound question.
        name = (
            "decide_template" if verb.endswith("-decide") else verb.rsplit("-", 1)[-1]
        )
        commands[name] = owner_command(ref, (str(PLUGIN / "crew"), verb, *arguments))
    return commands


def issued_commands(ref: Ref, action_id: str) -> dict[str, tuple[str, ...]]:
    # Command projection persists idempotent bindings so fresh shells can reload them.
    ref_path = _root(ref) / "ref.json"
    value = (
        review.review_ref_to_dict(ref)
        if isinstance(ref, review.ReviewRef)
        else measure.ref_to_dict(ref)
        if isinstance(ref, measure.MeasureRef)
        else build.ref_to_dict(ref)
    )
    measure._write_once(ref_path, measure._canonical(value), _root(ref))
    owner = (
        "review"
        if isinstance(ref, review.ReviewRef)
        else "measure"
        if isinstance(ref, measure.MeasureRef)
        else "build"
    )
    prefix = (
        str(PLUGIN / "scripts/openhands.py"),
        "run",
        "--context-file",
        str(context_path(ref)),
        "--",
    )
    flags = ("--owner", owner, "--ref-file", str(ref_path), "--action-id", action_id)
    return {name: (*prefix, name, *flags) for name in ("prepare", "capture", "recover")}


def _root(ref: Ref) -> Path:
    from state_discovery import crew_base

    owner = measure.sha256(measure._canonical(dataclasses.asdict(ref)))
    base = crew_base() / ".crew" / "openhands"
    return measure._safe_path(base / "transport" / owner, crew_base() / ".crew")


def _path(ref: Ref, action_id: str, suffix: str) -> Path:
    return measure._safe_path(
        _root(ref) / f"{measure.sha256(action_id.encode())}.{suffix}.json", _root(ref)
    )


def _claimed_item(ref: Ref, action_id: str) -> tuple[object, bool]:
    if isinstance(ref, build.BuildRef):

        def owned(_data: dict, journal: build.BuildJournal) -> tuple[object, bool]:
            action = journal.action
            if (
                journal.executor.host != "openhands"
                or not action
                or action.action_id != action_id
            ):
                raise review.WorkflowError(
                    "invalid_action", "not the owned OpenHands writer"
                )
            return build._item(ref, journal), action.status == "claimed"

        return build._transaction(ref, owned)
    if isinstance(ref, measure.MeasureRef):

        def owned_measure(
            _data: dict, journal: measure.MeasureJournal
        ) -> tuple[object, bool]:
            action = journal.action
            if (
                journal.advisor_channel != "openhands"
                or not action
                or action.item.action_id != action_id
            ):
                raise review.WorkflowError(
                    "invalid_action", "not the owned OpenHands advisor"
                )
            return action.item, action.status == "claimed"

        return measure._transaction(ref, owned_measure)
    run = review._guard_review_path(
        session_segment=ref.session_segment, run_id=ref.run_id, create=False
    )
    with review._owned_workflow_lock(run, ref):
        wf, _ = review._load_current_locked(ref, run)
        action = review._load_action(wf, action_id)
        if (
            action.get("channel") != "openhands"
            or action.get("driver") != "native"
            or review._action_attempt(action) != ref.attempt_id
        ):
            raise review.WorkflowError(
                "invalid_action", "not the owned OpenHands reviewer"
            )
        return review._work_item(action, ref), action["status"] == "claimed"


@contextlib.contextmanager
def _transport_lock() -> Iterator[None]:
    from state_discovery import crew_base

    if fcntl is None:
        raise review.WorkflowError(
            "unsupported_host_locking",
            "OpenHands transport requires POSIX fcntl locking",
        )
    base = crew_base() / ".crew"
    lock_path = measure._safe_path(base / "openhands" / "prepare.lock", base)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _refusal_path(ref: Ref, action_id: str, intent: dict) -> Path | None:
    reservation = intent.get("reservation_id")
    if reservation is None:
        return None
    if not isinstance(reservation, str) or not re.fullmatch(
        r"[0-9a-f]{32}", reservation
    ):
        raise review.WorkflowError(
            "invalid_native_binding", "invalid reservation identity"
        )
    return _path(ref, action_id, f"{reservation}.refused")


def _refused(intent_path: Path, intent: dict) -> bool:
    reservation = intent.get("reservation_id")
    if reservation is None:
        return False
    if not isinstance(reservation, str) or not re.fullmatch(
        r"[0-9a-f]{32}", reservation
    ):
        raise review.WorkflowError(
            "invalid_native_binding", "invalid reservation identity"
        )
    path = intent_path.with_name(
        intent_path.name.replace(".intent.json", f".{reservation}.refused.json")
    )
    if not path.exists():
        return False
    proof = read_json(path)
    if proof.get("intent") != intent or proof.get("reason") != "claim_refused":
        raise review.WorkflowError(
            "invalid_native_binding", "refusal differs from its reservation"
        )
    return True


def _record_refusal(ref: Ref, action_id: str, intent: dict, claim: dict) -> dict:
    if _owner_status(ref, action_id)[1] != "ready":
        return {
            **claim,
            "authorization": "do_not_spawn",
            "intent": str(_path(ref, action_id, "intent")),
        }
    proof = {"reason": "claim_refused", "intent": intent, "claim": claim}
    measure._write_once(
        _refusal_path(ref, action_id, intent), measure._canonical(proof), _root(ref)
    )
    return {**claim, "never_launched": True, "reason": "claim_refused"}


def _busy(session_id: str) -> bool:
    from state_discovery import crew_base

    root = crew_base() / ".crew" / "openhands" / "transport"
    for path in root.glob("*/*.intent.json"):
        if path.with_name(path.name.replace(".intent.json", ".retired.json")).exists():
            continue
        binding = path.parent / "context.json"
        if binding.exists():
            owner = parse_context(read_json(binding))
            if owner.session_id != session_id:
                continue
        intent = read_json(path)
        identity = intent.get("session_id")
        if not isinstance(identity, str) or not identity:
            raise review.WorkflowError(
                "invalid_host_evidence", "intent has no session identity"
            )
        if identity == session_id and not _refused(path, intent):
            return True
    return False


def prepare(ref: Ref, action_id: str) -> dict:
    """Reserve before claim; any interrupted reservation stays fenced."""
    host = validate_context(ref)
    with _transport_lock():
        intent_path = _path(ref, action_id, "intent")
        retry_refused = intent_path.exists() and _refused(
            intent_path, read_json(intent_path)
        )
        if intent_path.exists() and not retry_refused:
            return {"authorization": "do_not_spawn", "intent": str(intent_path)}
        if _busy(host.session_id):
            raise review.WorkflowError(
                "native_busy",
                "an invocation remains outstanding; capture or confirm quiescence first",
            )
        inactive, status = _owner_status(ref, action_id)
        if inactive or status not in {"ready", "claimed"}:
            return {"authorization": "do_not_spawn", "status": status}
        item, already_claimed = _claimed_item(ref, action_id)
        returned = (
            item.return_transport["primary"]["ingress_path"]
            if isinstance(ref, review.ReviewRef)
            else item.returned_path
        )
        metadata = launch_metadata(
            ref, action_id, item.role, item.model, None, item.prompt_path, returned
        )
        intent = {
            "reservation_id": uuid.uuid4().hex,
            "ref": dataclasses.asdict(ref),
            "owner_kind": type(ref).__name__,
            "action_id": action_id,
            "session_id": host.session_id,
            "context_sha256": measure.sha256(
                measure._canonical(dataclasses.asdict(host))
            ),
            "prompt_path": item.prompt_path,
            "prompt_sha256": measure.sha256(Path(item.prompt_path).read_bytes()),
            "task": metadata["task"],
        }
        if retry_refused:
            from models import atomic_write_json

            # Each refused reservation remains in its immutable proof; later claims get a new identity.
            atomic_write_json(intent_path, intent)
        else:
            measure._write_once(intent_path, measure._canonical(intent), _root(ref))
        if already_claimed:
            return {"authorization": "do_not_spawn", "intent": str(intent_path)}
        try:
            if isinstance(ref, build.BuildRef):
                claim = build.claim_build_action(ref, action_id)
            elif isinstance(ref, measure.MeasureRef):
                claim = measure.claim_measure_action(ref, action_id)
            else:
                result = review.claim_review_action(review.ClaimRequest(ref, action_id))
                claim = review.claim_response_to_dict(result)
        except review.ClaimRefused as exc:
            return _record_refusal(
                ref,
                action_id,
                intent,
                {
                    "authorization": "refused",
                    "code": exc.code,
                    "error": exc.error,
                    "message": exc.message,
                },
            )
        if claim.get("claim_state") == "unclaimed":
            return _record_refusal(ref, action_id, intent, claim)
        if claim["authorization"] != "spawn":
            return {**claim, "intent": str(intent_path)}
        return {
            "authorization": "spawn",
            "task": metadata["task"],
            "intent": str(intent_path),
        }


def _events(host: HostContext) -> Iterator[dict]:
    directory = Path(host.conversation_dir) / "events"
    if directory.is_symlink() or not directory.is_dir():
        raise review.WorkflowError(
            "missing_event_store",
            "native conversation event directory is unavailable in this workspace",
        )
    for path in directory.glob("event-*.json"):
        yield read_json(path)


def extract(ref: Ref, action_id: str) -> tuple[bytes, dict]:
    host = validate_context(ref)
    if _path(ref, action_id, "recovered").exists():
        raise review.WorkflowError(
            "stale_ref", "invocation was explicitly recovered; late output is rejected"
        )
    intent = read_intent(_path(ref, action_id, "intent"))
    if _refused(_path(ref, action_id, "intent"), intent):
        raise review.WorkflowError(
            "claim_refused",
            "this reservation was never launched; resolve the owner refusal and prepare again",
        )
    if (
        intent["ref"] != dataclasses.asdict(ref)
        or intent["action_id"] != action_id
        or intent["context_sha256"]
        != measure.sha256(measure._canonical(dataclasses.asdict(host)))
        or measure.sha256(Path(intent["prompt_path"]).read_bytes())
        != intent["prompt_sha256"]
    ):
        raise review.WorkflowError(
            "invalid_native_binding", "invocation context or owned prompt changed"
        )
    actions = list(
        islice(
            (
                event
                for event in _events(host)
                if event.get("kind") == "ActionEvent"
                and event.get("tool_name") == "task"
                and isinstance(event.get("action"), dict)
                and event["action"].get("prompt") == intent["task"]["prompt"]
            ),
            2,
        )
    )
    if not actions:
        raise review.WorkflowError(
            "native_launch_not_observed",
            "no matching persisted task invocation; inspect the host, never relaunch an uncertain claim",
        )
    if len(actions) != 1:
        raise review.WorkflowError(
            "ambiguous_native_launch",
            "expected exactly one persisted task invocation; never relaunch an uncertain claim",
        )
    action = actions[0]
    if any(
        not isinstance(action.get(key), str) or not action[key]
        for key in ("id", "tool_call_id")
    ):
        raise review.WorkflowError(
            "invalid_host_evidence", "task action lacks event/tool-call identity"
        )
    if (
        action["action"].get("subagent_type") != intent["task"]["subagent_type"]
        or action["action"].get("resume") is not None
    ):
        raise review.WorkflowError(
            "invalid_native_binding", "task role changed or task reused child history"
        )
    # Scaffold errors can be synthetic interrupt notices while workers still run.
    observations = list(
        islice(
            (
                event
                for event in _events(host)
                if (
                    event.get("kind") in {"ObservationEvent", "UserRejectObservation"}
                    and event.get("action_id") == action["id"]
                )
            ),
            2,
        )
    )
    if len(observations) != 1:
        raise review.WorkflowError(
            "completion_not_observed", "owned task has no unique terminal host event"
        )
    event = observations[0]
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise review.WorkflowError(
            "invalid_host_evidence", "task result lacks event identity"
        )
    if event.get("tool_name") != "task" or event.get("tool_call_id") != action.get(
        "tool_call_id"
    ):
        raise review.WorkflowError(
            "invalid_native_binding", "task tool-call identity differs"
        )
    task_id = None
    if event.get("kind") == "UserRejectObservation":
        reason = event.get("rejection_reason", "Host denied task")
        if not isinstance(reason, str):
            raise review.WorkflowError(
                "invalid_host_evidence", "rejection reason must be text"
            )
        content = reason.encode("utf-8")
        status = "failed"
    elif event.get("kind") == "ObservationEvent":
        observation = _object(event.get("observation"), "task observation")
        task_id = observation.get("task_id")
        if (
            observation.get("subagent") != intent["task"]["subagent_type"]
            or not isinstance(task_id, str)
            or not task_id
        ):
            raise review.WorkflowError(
                "invalid_native_binding",
                "task result has no matching role or task identity",
            )
        if observation.get("status") not in {"completed", "error"}:
            raise review.WorkflowError(
                "completion_not_observed", "task is not terminal"
            )
        blocks = observation.get("content")
        if not isinstance(blocks, list) or any(
            not isinstance(b, dict)
            or b.get("type") != "text"
            or not isinstance(b.get("text"), str)
            for b in blocks
        ):
            raise review.WorkflowError(
                "invalid_native_result", "task report must be structured text content"
            )
        content = "".join(block["text"] for block in blocks).encode("utf-8")
        status = (
            "ok"
            if observation["status"] == "completed"
            and not observation.get("is_error", False)
            else "failed"
        )
        if status == "ok" and (
            task_id == "unknown"
            or not content.strip()
            or content == b"Task completed with no result."
        ):
            raise review.WorkflowError(
                "invalid_native_result", "completed task has no owned final report"
            )
    else:
        raise review.WorkflowError(
            "completion_not_observed", "unsupported host terminal event"
        )
    source = {
        "ref": dataclasses.asdict(ref),
        "action_id": action_id,
        "backend_id": host.backend_id,
        "conversation_id": host.conversation_id,
        "task_id": task_id,
        "action_event_id": action["id"],
        "observation_event_id": event["id"],
        "tool_call_id": action["tool_call_id"],
        "action_sha256": measure.sha256(measure._canonical(action)),
        "observation_sha256": measure.sha256(measure._canonical(event)),
        "returned_sha256": measure.sha256(content),
        "status": status,
    }
    return content, source


def require_event_capture(
    ref: Ref, action_id: str, content: bytes, status: str
) -> None:
    actual, source = extract(ref, action_id)
    if (
        content != actual
        or source["status"] != status
        or read_json(_path(ref, action_id, "source")) != source
    ):
        raise review.WorkflowError(
            "invalid_native_result",
            "capture differs from the owned persisted host event",
        )


def _owner_status(ref: Ref, action_id: str) -> tuple[bool, str]:
    """Read settlement without deriving work or requiring an active loop owner."""
    import loop_state
    from models import state_lock
    from state_discovery import is_active_value

    if isinstance(ref, (build.BuildRef, measure.MeasureRef)):

        def status(data: dict, journal: object) -> tuple[bool, str]:
            accepted = any(
                result.action_id == action_id for result in journal.accepted_actions
            )
            action = journal.action
            identity = (
                action.action_id
                if isinstance(ref, build.BuildRef) and action
                else action.item.action_id
                if action
                else None
            )
            if identity != action_id and not accepted:
                raise review.WorkflowError(
                    "invalid_action", "owner no longer retains this action"
                )
            return not is_active_value(
                data.get("active")
            ), "settled" if accepted else action.status

        try:
            return (
                build._transaction(ref, status)
                if isinstance(ref, build.BuildRef)
                else measure._transaction(ref, status, active=False)
            )
        except review.WorkflowError as exc:
            if exc.code != "stale_owner":
                raise
            # The immutable intent still owns the host invocation, never the replacement loop.
            return True, "superseded"
    run = review._guard_review_path(
        session_segment=ref.session_segment, run_id=ref.run_id, create=False
    )
    peek = review._workflow(run)
    identity = peek["workflow_identity"]
    binding = (
        review.parse_loop_binding(identity["loop_binding"])
        if identity["kind"] == "loop_review"
        else None
    )
    with contextlib.ExitStack() as stack:
        if binding:
            stack.enter_context(
                state_lock(loop_state.resolve(binding.loop, binding.session_segment))
            )
        stack.enter_context(review._workflow_lock(run))
        wf = review._workflow(run)
        review._verify_host(wf)
        current = review.parse_review_ref(wf["ref"])
        if dataclasses.replace(current, attempt_id=ref.attempt_id) != ref:
            raise review.WorkflowError("stale_ref", "review owner identity differs")
        if wf["workflow_identity"] != identity:
            raise review.WorkflowError("stale_owner", "review owner identity changed")
        action = review._load_action(wf, action_id)
        if review._action_attempt(action) != ref.attempt_id:
            raise review.WorkflowError("stale_ref", "action belongs to another attempt")
        if current != ref:
            receipt = review._source_receipt(wf["retry_receipts"], ref.attempt_id)
            if (
                review._attempt_number(ref.attempt_id)
                >= review._attempt_number(current.attempt_id)
                or receipt is None
                or action["status"] != "settled"
            ):
                raise review.WorkflowError(
                    "stale_ref", "historical action lacks settled retry authority"
                )
            if action.get("accepted_path") is not None:
                review._read_accepted_bytes(action, run)
        inactive = False
        if binding:
            owner = loop_state.read(
                loop_state.resolve(binding.loop, binding.session_segment)
            )
            if (
                owner.get("loop_instance_id") != binding.loop_instance_id
                or owner.get("session_id") != binding.session_segment
            ):
                return True, "superseded"
            inactive = not is_active_value(owner.get("active"))
        return inactive, action["status"]


def _retire(ref: Ref, action_id: str, reason: str) -> dict:
    result = {
        "authorization": "do_not_spawn",
        "status": "retired",
        "reason": reason,
        "ref": dataclasses.asdict(ref),
        "action_id": action_id,
    }
    measure._write_once(
        _path(ref, action_id, "retired"), measure._canonical(result), _root(ref)
    )
    return result


def read_intent(path: Path) -> dict:
    intent = read_json(path)
    required = {
        "ref",
        "action_id",
        "session_id",
        "context_sha256",
        "prompt_path",
        "prompt_sha256",
        "task",
    }
    missing = required - intent.keys()
    if missing:
        raise review.WorkflowError(
            "invalid_native_binding",
            f"intent {path} lacks keys: {', '.join(sorted(missing))}",
        )
    return intent


def _validate_intent(ref: Ref, action_id: str) -> dict:
    host = context(validate=False)
    intent = read_intent(_path(ref, action_id, "intent"))
    if (
        intent["ref"] != dataclasses.asdict(ref)
        or intent["action_id"] != action_id
        or intent["session_id"] != host.session_id
        or intent["context_sha256"]
        != measure.sha256(measure._canonical(dataclasses.asdict(host)))
    ):
        raise review.WorkflowError(
            "invalid_native_binding",
            "recovery does not own the frozen invocation context",
        )
    return intent


def capture(ref: Ref, action_id: str) -> object:
    from multiagent import workflow_transport as transport

    with _transport_lock():
        _validate_intent(ref, action_id)
        content, source = extract(ref, action_id)
        measure._write_once(
            _path(ref, action_id, "source"), measure._canonical(source), _root(ref)
        )
        inactive, status = _owner_status(ref, action_id)
        if _path(ref, action_id, "retired").exists():
            return read_json(_path(ref, action_id, "retired"))
        if status in {"settled", "recovered"}:
            return _retire(ref, action_id, "captured")
        if inactive:
            if isinstance(ref, build.BuildRef) and status == "claimed":
                build.recover_build_action(ref, action_id, "not_running")
            return _retire(ref, action_id, "completed_after_cancellation")
        diagnostic = (
            None
            if source["status"] == "ok"
            else "Native OpenHands task failed or was denied; see retained source event"
        )
        kwargs = {"status": source["status"], "diagnostic": diagnostic}
        if isinstance(ref, build.BuildRef):
            result = build.capture_build_return(ref, action_id, content, **kwargs)
        elif isinstance(ref, measure.MeasureRef):
            result = transport.capture_measure_return(ref, action_id, content, **kwargs)
        else:
            result = transport.capture_review_return(ref, action_id, content, **kwargs)
        _retire(ref, action_id, "captured")
        return result


def recover(ref: Ref, action_id: str, confirmation: str) -> object:
    """Retain quiescence before owner settlement; reconcile either interrupted write."""
    if confirmation != "not_running":
        raise review.WorkflowError(
            "not_confirmed", "confirm actual native task quiescence before recovery"
        )
    with _transport_lock():
        intent = _validate_intent(ref, action_id)
        if _refused(_path(ref, action_id, "intent"), intent):
            proof = read_json(_refusal_path(ref, action_id, intent))
            return {**proof["claim"], "never_launched": True, "reason": "claim_refused"}
        retired = _path(ref, action_id, "retired")
        if retired.exists():
            return read_json(retired)
        measure._write_once(
            _path(ref, action_id, "recovered"),
            measure._canonical(
                {
                    "ref": dataclasses.asdict(ref),
                    "action_id": action_id,
                    "confirmation": confirmation,
                }
            ),
            _root(ref),
        )
        inactive, status = _owner_status(ref, action_id)
        if status in {"ready", "claimed"} and (
            not inactive or isinstance(ref, build.BuildRef)
        ):
            if isinstance(ref, build.BuildRef):
                result = build.recover_build_action(
                    ref, action_id, confirmation, reserved=True
                )
            elif isinstance(ref, measure.MeasureRef):
                result = measure.recover_measure_action(
                    measure.MeasureRecovery(ref, action_id, confirmation), reserved=True
                )
            else:
                result = review.recover_review_action(
                    review.RecoveryRequest(
                        ref, action_id, confirmation, "native_task_lost"
                    ),
                    reserved=True,
                )
        else:
            result = None
        retirement = _retire(ref, action_id, "quiescent")
        return result if result is not None else retirement
