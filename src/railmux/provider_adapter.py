"""Provider-neutral adapter layer consolidating Claude, Codex, and Devin dispatch.

Instead of scattering ``if session_type == ...`` branches across ``App``, each
provider registers one immutable adapter describing its metadata, command
builders, auto-run policy metadata, preview source resolution, and delete copy.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from railmux import devin_index
from railmux.launcher import (
    build_codex_new_command,
    build_codex_resume_command,
    build_new_session_command,
    build_resume_command,
)

if TYPE_CHECKING:
    from railmux.models import SessionMeta
    from railmux.ui.app import App


@dataclass(frozen=True)
class AutoRunSpec:
    """Configuration and UI copy for one provider's auto-run / bypass prompt."""

    policy_attr: str
    setter_name: str
    runtime_attr: str
    handled_attr: str
    label: str
    modal_title: str
    modal_heading: str
    modal_warning: str


@dataclass(frozen=True)
class ProviderAdapter:
    """Consolidated runtime contract for one registered agent provider."""

    key: str
    label: str
    short_label: str
    preview_format: str
    auto_run: AutoRunSpec | None
    build_new: Callable[[App, str, Path], tuple[list[str], dict[str, str] | None]]
    build_resume: Callable[[App, str, SessionMeta, Path], tuple[list[str], dict[str, str] | None]]
    lease_root: Callable[[App], Path]
    preview_path: Callable[[App, SessionMeta], Path | None]
    delete_session_detail: Callable[[int], str]
    delete_running_detail: Callable[[int], str]


def _claude_new(_app: App, binary: str, cwd: Path) -> tuple[list[str], dict[str, str] | None]:
    return build_new_session_command(claude_binary=binary, cwd=cwd), None


def _claude_resume(
    _app: App, binary: str, session: SessionMeta, cwd: Path,
) -> tuple[list[str], dict[str, str] | None]:
    return build_resume_command(
        claude_binary=binary, session_id=session.session_id, cwd=cwd,
    ), None


def _claude_lease_root(app: App) -> Path:
    return getattr(app, "_claude_home", Path.home() / ".claude")


def _claude_preview(_app: App, session: SessionMeta) -> Path | None:
    return session.jsonl_path if session.jsonl_path != Path() else None


def _codex_new(app: App, binary: str, cwd: Path) -> tuple[list[str], dict[str, str] | None]:
    return build_codex_new_command(
        codex_binary=binary, cwd=cwd, yolo=app._codex_yolo_enabled(),
    ), app._codex_env()


def _codex_resume(
    app: App, binary: str, session: SessionMeta, cwd: Path,
) -> tuple[list[str], dict[str, str] | None]:
    return build_codex_resume_command(
        codex_binary=binary,
        session_id=session.session_id,
        cwd=cwd,
        yolo=app._codex_yolo_enabled(),
    ), app._codex_env()


def _codex_lease_root(app: App) -> Path:
    config = getattr(app, "_config", None)
    return config.resolved_codex_home() if config is not None else Path.home() / ".codex"


def _codex_preview(app: App, session: SessionMeta) -> Path | None:
    representative = app._codex_representative(session.session_id)
    target = representative or session
    return target.jsonl_path if target.jsonl_path != Path() else None


def _devin_new(app: App, binary: str, _cwd: Path) -> tuple[list[str], dict[str, str] | None]:
    return devin_index.command(binary, bypass=app._devin_bypass_enabled()), None


def _devin_resume(
    _app: App, binary: str, session: SessionMeta, _cwd: Path,
) -> tuple[list[str], dict[str, str] | None]:
    return devin_index.command(binary, session.session_id), None


def _devin_lease_root(_app: App) -> Path:
    return devin_index.lease_root()


def _devin_preview(_app: App, session: SessionMeta) -> Path | None:
    return devin_index.preview_source(session.session_id)


def _file_delete_detail(lineage_count: int) -> str:
    if lineage_count > 1:
        return (
            f"The logical session and its {lineage_count} rewind records "
            "will be permanently removed from disk.\n"
            "Its background tmux session will also be killed."
        )
    return (
        "The session file will be permanently removed from disk.\n"
        "Its background tmux session will also be killed."
    )


def _file_running_delete_detail(lineage_count: int) -> str:
    if lineage_count > 1:
        return (
            "The detached tmux session will be killed.\n"
            f"All {lineage_count} rewind records will also be "
            "permanently deleted from disk."
        )
    return (
        "The detached tmux session will be killed.\n"
        "The session file will also be permanently deleted from disk."
    )


def _devin_delete_detail(_lineage_count: int) -> str:
    return (
        "The Devin session will be permanently deleted.\n"
        "Its background tmux session will also be killed."
    )


def _devin_running_delete_detail(_lineage_count: int) -> str:
    return (
        "The detached tmux session will be killed.\n"
        "The Devin session will also be permanently deleted."
    )


CLAUDE_ADAPTER = ProviderAdapter(
    key="claude",
    label="Claude",
    short_label="Claude",
    preview_format="claude",
    auto_run=None,
    build_new=_claude_new,
    build_resume=_claude_resume,
    lease_root=_claude_lease_root,
    preview_path=_claude_preview,
    delete_session_detail=_file_delete_detail,
    delete_running_detail=_file_running_delete_detail,
)

CODEX_ADAPTER = ProviderAdapter(
    key="codex",
    label="Codex",
    short_label="Codex",
    preview_format="codex",
    auto_run=AutoRunSpec(
        policy_attr="codex_yolo_policy",
        setter_name="set_codex_yolo_policy",
        runtime_attr="_codex_yolo_runtime",
        handled_attr="_codex_yolo_prompt_handled",
        label="Codex auto-run",
        modal_title="Codex auto-run",
        modal_heading="Enable Codex auto-run (YOLO)?",
        modal_warning=(
            "Codex will run shell commands WITHOUT approval prompts and "
            "WITHOUT sandboxing — full access to your files. Only enable "
            "this if you trust what you run here."
        ),
    ),
    build_new=_codex_new,
    build_resume=_codex_resume,
    lease_root=_codex_lease_root,
    preview_path=_codex_preview,
    delete_session_detail=_file_delete_detail,
    delete_running_detail=_file_running_delete_detail,
)

DEVIN_ADAPTER = ProviderAdapter(
    key="devin",
    label="Devin",
    short_label="Devin",
    preview_format="devin",
    auto_run=AutoRunSpec(
        policy_attr="devin_bypass_policy",
        setter_name="set_devin_bypass_policy",
        runtime_attr="_devin_bypass_runtime",
        handled_attr="_devin_bypass_prompt_handled",
        label="Devin bypass mode",
        modal_title="Devin bypass mode",
        modal_heading="Start new Devin chats in bypass mode?",
        modal_warning=(
            "New Devin conversations will auto-approve all tools "
            "(writes and shell commands) without prompts. Resumed "
            "conversations keep their saved mode. Only enable this "
            "if you trust what you run here."
        ),
    ),
    build_new=_devin_new,
    build_resume=_devin_resume,
    lease_root=_devin_lease_root,
    preview_path=_devin_preview,
    delete_session_detail=_devin_delete_detail,
    delete_running_detail=_devin_running_delete_detail,
)

PROVIDER_ADAPTERS: dict[str, ProviderAdapter] = {
    adapter.key: adapter
    for adapter in (CLAUDE_ADAPTER, CODEX_ADAPTER, DEVIN_ADAPTER)
}


def adapter_for(session_type: str) -> ProviderAdapter:
    """Return the registered adapter for *session_type*, defaulting to Claude."""
    return PROVIDER_ADAPTERS.get(session_type, CLAUDE_ADAPTER)
