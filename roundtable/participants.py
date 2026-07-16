"""Participant roster and provider registry for a collaboration run."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .adapters import Adapter, AdapterError
from .adapters.claude import INSTALL_HINT as CLAUDE_HINT
from .adapters.claude import ClaudeAdapter
from .adapters.codex import INSTALL_HINT as CODEX_HINT
from .adapters.codex import CodexAdapter

Role = Literal["leader", "reviewer"]


@dataclass(frozen=True)
class Provider:
    name: str
    adapter: type[Adapter]
    command: str
    install_hint: str


@dataclass(frozen=True)
class ParticipantSpec:
    role: Role
    provider: str
    name: str
    model: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


PROVIDERS = {
    "claude": Provider("claude", ClaudeAdapter, "claude", CLAUDE_HINT),
    "codex": Provider("codex", CodexAdapter, "codex", CODEX_HINT),
}


def provider_names() -> tuple[str, ...]:
    return tuple(PROVIDERS)


def build_roster(
    lead: str,
    reviewer: str | None = None,
    *,
    lead_name: str | None = None,
    reviewer_name: str | None = None,
    lead_model: str | None = None,
    reviewer_model: str | None = None,
    provider_models: dict[str, str | None] | None = None,
) -> tuple[ParticipantSpec, ParticipantSpec]:
    """Resolve providers, display names, and role-specific models in one place."""
    if lead not in PROVIDERS:
        raise ValueError(f"unknown leader provider: {lead}")
    reviewer = reviewer or ("codex" if lead == "claude" else "claude")
    if reviewer not in PROVIDERS:
        raise ValueError(f"unknown reviewer provider: {reviewer}")

    duplicate = lead == reviewer
    lead_label = lead_name or (f"{lead}-leader" if duplicate else lead)
    reviewer_label = reviewer_name or (f"{reviewer}-reviewer" if duplicate else reviewer)
    defaults = provider_models or {}
    return (
        ParticipantSpec("leader", lead, lead_label, lead_model or defaults.get(lead)),
        ParticipantSpec(
            "reviewer", reviewer, reviewer_label,
            reviewer_model or defaults.get(reviewer),
        ),
    )


def create_participant(
    spec: ParticipantSpec,
    *,
    cwd: str,
    mode: str,
    dangerous: bool,
    timeout: int,
) -> Adapter:
    """Create one independent stateful adapter from a participant specification."""
    provider = PROVIDERS.get(spec.provider)
    if provider is None:
        raise AdapterError(f"unknown participant provider: {spec.provider}")
    adapter = provider.adapter(
        cwd=cwd,
        model=spec.model,
        writable=(spec.role == "leader" and mode == "build"),
        dangerous=dangerous,
        timeout=timeout,
    )
    adapter.name = spec.name
    return adapter
