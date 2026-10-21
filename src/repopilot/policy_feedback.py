from __future__ import annotations

import hashlib

from repopilot.models import CodeChangeOutput, PolicyFeedback
from repopilot.repository import RepositoryContext
from repopilot.security import (
    CapabilityAction,
    CapabilityError,
    CapabilityReason,
    SecurityError,
    normalize_workspace_path,
    redact_secrets,
)

CODER_INTENT_SCHEMA_VERSION = "CodeChangeOutput-v2"


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_modification_intent(
    changes: CodeChangeOutput, context: RepositoryContext
) -> None:
    """Validate model preconditions before the workspace's atomic batch preflight."""

    editable = set(context.editable_paths or ())
    for edit in changes.edits:
        path = edit.path
        if edit.action != "write":
            raise CapabilityError(
                CapabilityReason.ACTION_NOT_SUPPORTED,
                CapabilityAction.CREATE,
                path,
                "the coding graph accepts only existing-file write intents",
                policy_version=context.capability_policy_version,
            )
        if path not in context.files:
            raise CapabilityError(
                CapabilityReason.OUTSIDE_CONTEXT,
                CapabilityAction.WRITE,
                path,
                "write intent is outside the current readable context",
                policy_version=context.capability_policy_version,
            )
        if path not in editable:
            raise CapabilityError(
                CapabilityReason.WRITE_DENIED,
                CapabilityAction.WRITE,
                path,
                "write intent is outside the current editable paths",
                policy_version=context.capability_policy_version,
            )
        if edit.expected_content_sha256 is None:
            raise CapabilityError(
                CapabilityReason.EXPECTED_CONTENT_REQUIRED,
                CapabilityAction.WRITE,
                path,
                "write intent requires a current content digest",
                policy_version=context.capability_policy_version,
            )
        if edit.expected_content_sha256 != content_sha256(context.files[path]):
            raise CapabilityError(
                CapabilityReason.STALE_CONTENT,
                CapabilityAction.WRITE,
                path,
                "write intent uses a stale content digest",
                policy_version=context.capability_policy_version,
            )


def _safe_context_path(path: str, context: RepositoryContext) -> str | None:
    if path not in context.files or redact_secrets(path) != path:
        return None
    try:
        normalized = normalize_workspace_path(path)
    except SecurityError:
        return None
    return path if normalized.value == path and all(ord(ch) >= 32 for ch in path) else None


def normalized_policy_feedback(
    error: SecurityError, context: RepositoryContext
) -> PolicyFeedback:
    """Never expose exception text or untrusted path values to the model."""

    reason = error.reason if isinstance(error, CapabilityError) else CapabilityReason.INVALID_PATH
    target = _safe_context_path(error.path, context) if isinstance(error, CapabilityError) else None
    editable = context.editable_paths or ()
    allowed = [path for path in editable if _safe_context_path(path, context) is not None][:32]
    guidance = (
        "The complete proposal was rejected; no edits were applied. Re-read the latest "
        "context and submit only write intents for exact allowed paths with current "
        "expected_content_sha256 values."
    )
    return PolicyFeedback(
        reason_code=reason.value,
        target_path=target,
        allowed_paths=allowed,
        guidance=guidance,
    )
