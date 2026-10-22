"""Public diagnostics and private execution data have different contracts.

Never run a lossy public projection over an artifact, edit, manifest or checkpoint.
Known secrets in executable data cause rejection before a checkpoint/model call.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from repopilot.security import SecurityError, contains_known_secret, redact_secrets

PUBLIC_DATA_VERSION = "public-data-v1"
MAX_PUBLIC_TEXT = 2_000
MAX_PUBLIC_ITEMS = 200
MAX_PUBLIC_DEPTH = 12
_CREDENTIAL_KEYS = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie", "apikey",
    "accesstoken", "refreshtoken", "idtoken", "token", "password", "passwd",
    "clientsecret", "secretkey", "githubtoken", "openaiapikey",
})
_PRIVATE_KEYS = frozenset({
    "headers", "requestheaders", "responseheaders", "raw", "rawresponse",
    "providerresponse", "providerexception", "traceback", "excinfo", "prompt",
    "messages", "researchfiles", "pendingchanges", "edits", "source", "content",
    "diff", "diffpreview", "workspace",
})


class SensitiveExecutionData(SecurityError):
    code = "sensitive_execution_data"

    def __init__(self) -> None:
        super().__init__(self.code)


class PublicExecutionError(RuntimeError):
    """A node failure that can be checkpointed without its provider exception."""

    def __init__(self, error: BaseException):
        diagnostic = safe_error(error)
        self.code = diagnostic["code"]
        self.error_type = diagnostic["type"]
        super().__init__(f"{self.code} ({self.error_type})")


def public_text(value: str, limit: int = MAX_PUBLIC_TEXT) -> str:
    # Scan before truncation: a token straddling the output limit must not survive.
    return redact_secrets(value).replace("\x00", "")[:limit]


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def public_json(value: Any, *, _depth: int = 0) -> Any:
    """Bounded JSON copy; never stringify unknown objects or exceptions."""
    if _depth >= MAX_PUBLIC_DEPTH:
        return "[LIMIT]"
    if isinstance(value, Mapping):
        result = {}
        for raw_key, item in list(value.items())[:MAX_PUBLIC_ITEMS]:
            if not isinstance(raw_key, str):
                continue
            name = _key(raw_key)
            if name in _PRIVATE_KEYS:
                continue
            key = public_text(raw_key, 120)
            result[key] = (
                "[REDACTED]" if name in _CREDENTIAL_KEYS
                else public_json(item, _depth=_depth + 1)
            )
        return result
    if isinstance(value, (tuple, list)):
        return [public_json(item, _depth=_depth + 1) for item in value[:MAX_PUBLIC_ITEMS]]
    if isinstance(value, str):
        return public_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[UNSUPPORTED]"


def _fields(value: Any, names: set[str] | frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return public_json({key: item for key, item in value.items() if key in names})


_REVIEW = frozenset({"approved", "summary", "feedback", "risk_level"})
_TEST = frozenset({
    "command", "exit_code", "stdout", "stderr", "duration_ms", "timed_out", "image_id",
    "policy_version",
})
_RETRIEVAL = frozenset({
    "strategy", "query_terms", "candidate_count", "selected_files", "selected_chars",
    "skipped_for_budget", "editable_files", "capability_policy_version", "evidence",
})
_RESULT = frozenset({
    "status", "iteration", "policy_retry_count", "policy_feedback", "changed_files",
    "initial_retrieval", "retrieval", "review", "test_result", "node_metrics",
    "pull_request_url", "error", "error_code", "candidate_id", "generation", "evidence_digest",
    "approval_decision_id", "approval_id", "approval_request_version", "publish_receipt",
    "publish_reason",
})
_EVENT = frozenset({
    "repository_url", "issue_title", "steps", "risks", "files", "query_terms", "strategy",
    "candidate_count", "selected_chars", "skipped_for_budget", "editable_files",
    "capability_policy_version", "evidence", "duration_ms", "edit_count", "error_code",
    "feedback", "retry_available", "changed_files", "iteration_changes", "summary",
    "policy_retry_count", "candidate_id", "evidence_digest", "exit_code", "timed_out",
    "approved", "pull_request_url", "branch", "publish_reason", "publish_receipt",
    "iteration", "policy_feedback", "requests", "reason", "confirmed", "resources", "scope",
    "schema_version", "decision_id", "approval_id", "request_version", "actor", "decided_at",
    "dispatch_state", "publication_protocol_version", "status",
})
_INTERRUPT = frozenset({
    "schema_version", "candidate_id", "artifact_digest", "base_sha", "evidence_digest",
    "candidate_url", "question", "changed_files", "review", "test_result",
})
_METRIC = frozenset({"node", "duration_ms", "iteration"})
_POLICY = frozenset({
    "schema_version", "reason_code", "target_path", "allowed_actions", "allowed_paths", "guidance",
})
_HIT = frozenset({
    "path", "score", "lexical_score", "path_score", "symbol_score", "role_score",
    "dependency_score", "matched_terms", "matched_symbols", "related_paths", "content_chars",
})
_RECEIPT = frozenset({
    "schema_version", "publish_intent_id", "branch", "version", "phase", "step", "steps",
    "remote_ids", "reason", "cancel_requested", "requires_revalidation", "observed_base_shas",
})


def public_receipt(value: Any) -> dict[str, Any]:
    result = _fields(value, _RECEIPT)
    remote = result.get("remote_ids", {})
    result["remote_ids"] = {
        key: item for key, item in remote.items()
        if key in {"tree_sha", "commit_sha", "ref", "pr_number", "pull_request_url"}
        or re.fullmatch(r"blob:[0-9a-f]{40}", key)
    } if isinstance(remote, Mapping) else {}
    steps = result.get("steps", {})
    result["steps"] = {
        key: item for key, item in steps.items()
        if key in {"tree", "commit", "ref", "pr"} or re.fullmatch(r"blob:[0-9a-f]{40}", key)
    } if isinstance(steps, Mapping) else {}
    return result


def public_result(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result = _fields(value, _RESULT)
    for name in ("initial_retrieval", "retrieval"):
        if name in result:
            result[name] = _fields(result[name], _RETRIEVAL)
            if isinstance(result[name].get("evidence"), list):
                result[name]["evidence"] = [
                    _fields(item, _HIT) for item in result[name]["evidence"]
                ]
    if "review" in result:
        result["review"] = _fields(result["review"], _REVIEW)
    if "test_result" in result:
        result["test_result"] = _fields(result["test_result"], _TEST)
    if result.get("error"):
        result["error"] = public_error_code(result["error"])
    if isinstance(result.get("node_metrics"), list):
        result["node_metrics"] = [_fields(item, _METRIC) for item in result["node_metrics"]]
    if result.get("policy_feedback") is not None:
        result["policy_feedback"] = _fields(result["policy_feedback"], _POLICY)
    if result.get("publish_receipt") is not None:
        result["publish_receipt"] = public_receipt(result["publish_receipt"])
    return result


def public_event_payload(value: Any) -> dict[str, Any]:
    result = _fields(value, _EVENT)
    if isinstance(result.get("requests"), list):
        result["requests"] = [_fields(item, _INTERRUPT) for item in result["requests"]]
        for item in result["requests"]:
            if "review" in item:
                item["review"] = _fields(item["review"], _REVIEW)
            if "test_result" in item:
                item["test_result"] = _fields(item["test_result"], _TEST)
    if isinstance(result.get("evidence"), list):
        result["evidence"] = [_fields(item, _HIT) for item in result["evidence"]]
    if isinstance(result.get("feedback"), Mapping):
        result["feedback"] = _fields(result["feedback"], _POLICY)
    if result.get("publish_receipt") is not None:
        result["publish_receipt"] = public_receipt(result["publish_receipt"])
    return result


def public_error_code(value: Any) -> str:
    if (
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value)
        and not contains_known_secret(value)
    ):
        return value
    return "execution_failed"


def require_secret_free(value: Any) -> None:
    """Inspect without modifying recoverable code or content identities."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                isinstance(key, str) and _key(key) in _CREDENTIAL_KEYS
                and item not in (None, "", "[REDACTED]")
            ):
                raise SensitiveExecutionData()
            if isinstance(key, str) and contains_known_secret(key):
                raise SensitiveExecutionData()
            require_secret_free(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            require_secret_free(item)
    elif isinstance(value, str) and contains_known_secret(value):
        raise SensitiveExecutionData()


def safe_error(error: BaseException) -> dict[str, str]:
    error_type = type(error).__name__
    if isinstance(error, PublicExecutionError):
        error_type = error.error_type
    if (
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type)
        or contains_known_secret(error_type)
    ):
        error_type = "Exception"
    code = "execution_failed"
    # Only application errors can supply a code. Provider exception attributes
    # and messages are never serialized.
    if type(error).__module__.startswith("repopilot."):
        candidate = getattr(error, "code", "")
        if (
            isinstance(candidate, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", candidate)
            and not contains_known_secret(candidate)
        ):
            code = candidate
    elif isinstance(error, TimeoutError):
        code = "operation_timeout"
    elif isinstance(error, ConnectionError):
        code = "transport_unavailable"
    return {"code": code, "type": error_type}


class PublicLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        diagnostic = safe_error(record.exc_info[1]) if record.exc_info else None
        if diagnostic:
            record.msg = "Operation failed"
        else:
            if isinstance(record.msg, Mapping):
                record.msg = str(public_json(record.msg))
            if isinstance(record.args, tuple):
                record.args = tuple(
                    safe_error(item) if isinstance(item, BaseException)
                    else public_json(item) if isinstance(item, Mapping) else item
                    for item in record.args
                )
            elif isinstance(record.args, Mapping):
                projected = public_json(record.args)
                record.args = {key: projected.get(key, "[PRIVATE]") for key in record.args}
            record.msg = public_text(record.getMessage(), 4_000)
        record.msg = record.msg.replace("\r", r"\r").replace("\n", r"\n")
        record.args = ()
        # Do not format a provider traceback (which may include request/response data).
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        if diagnostic:
            record.msg += f" error_code={diagnostic['code']} error_type={diagnostic['type']}"
        return True


def configure_public_logging() -> None:
    """Apply once per existing handler, including framework/library handlers."""
    loggers = [logging.getLogger(), *(
        logger for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, PublicLogFilter) for item in handler.filters):
                handler.addFilter(PublicLogFilter())
