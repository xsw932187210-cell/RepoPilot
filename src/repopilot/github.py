"""Approved GitHub publication with durable, conservative remote reconciliation."""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote
from weakref import WeakValueDictionary

import httpx

from repopilot.approvals import ApprovalConflict
from repopilot.config import Settings
from repopilot.execution import ExecutionStopped, current_control
from repopilot.generations import GenerationConflict
from repopilot.publication import (
    PublicationConflict,
    PublicationStore,
    PublishIntent,
    PublishReceipt,
    artifact_objects,
    commit_oid,
    commit_payload,
    git_tree,
)
from repopilot.security import SecurityError, validate_repository_url

CancelCheck = Callable[[], Awaitable[bool]]


@dataclass(slots=True)
class PublishResult:
    published: bool
    pull_request_url: str | None = None
    branch: str | None = None
    reason: str | None = None
    receipt: PublishReceipt | None = None


class RemoteUnknown(RuntimeError):
    pass


class GitHubPublisher:
    """One executor per intent; receipt CAS is not an external-request fence."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        store: PublicationStore | None = None,
    ):
        self.settings, self.transport, self.store = settings, transport, store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def disabled_reason(self, repository_url: str = "") -> str | None:
        if repository_url.startswith("demo://"):
            return "Demo runs never write to GitHub"
        if not self.settings.github_write_enabled:
            return "GitHub writes are disabled"
        if not self.settings.github_token:
            return "GITHUB_TOKEN is not configured"
        return None

    async def publish(
        self,
        *,
        intent_id: str,
        cancelled: CancelCheck | None = None,
        reconcile_only: bool = False,
    ) -> PublishResult:
        if self.store is None:
            raise PublicationConflict("persistent_publish_intent_required")
        lock = self._locks.setdefault(intent_id, asyncio.Lock())
        async with lock:
            intent, receipt, candidate = await self.store.load(intent_id)
            reason = self.disabled_reason()
            if reason and not reconcile_only:
                return PublishResult(
                    receipt.published,
                    receipt.pull_request_url,
                    intent.branch,
                    reason,
                    receipt,
                )
            owner, _ = validate_repository_url(f"https://github.com/{intent.target_repo}")
            if owner.lower() not in self.settings.allowed_github_owners:
                raise SecurityError("GitHub owner is not allowlisted")
            entries, blobs = artifact_objects(self.store.artifacts, candidate)
            if git_tree(entries) != intent.tree_sha or commit_oid(intent) != intent.commit_sha:
                raise PublicationConflict("publication_object_identity_mismatch")
            # First requests require current authority. Replays with possible effects may
            # still read those effects after cancellation or task failure.
            if not receipt.steps and not reconcile_only:
                await self.store.authorize(intent)
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.settings.github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            async with httpx.AsyncClient(
                headers=headers,
                timeout=30,
                transport=self.transport,
                base_url=f"https://api.github.com/repos/{intent.target_repo}/",
            ) as client:
                execution = _Publication(
                    self.store,
                    client,
                    intent,
                    receipt,
                    cancelled,
                    reconcile_only,
                )
                await execution.run(entries, blobs)
                receipt = execution.receipt
            return PublishResult(
                receipt.published,
                receipt.pull_request_url,
                intent.branch,
                receipt.reason,
                receipt,
            )


class _Publication:
    def __init__(
        self,
        store: PublicationStore,
        client: httpx.AsyncClient,
        intent: PublishIntent,
        receipt: PublishReceipt,
        cancelled: CancelCheck | None,
        reconcile_only: bool,
    ):
        self.store, self.client, self.intent = store, client, intent
        self.receipt, self.cancelled = receipt, cancelled
        self.reconcile_only = reconcile_only

    async def save(self, **changes: Any) -> None:
        self.receipt = await self.store.save(self.receipt.model_copy(update=changes, deep=True))

    async def get(self, path: str, *, params: dict | None = None) -> Any | None:
        try:
            control = current_control()
            timeout = control.reconciliation_timeout(30) if control else 30
            if timeout <= 0:
                raise RemoteUnknown("publication_reconciliation_deadline")
            request = self.client.get(path, params=params, timeout=timeout)
            response = (
                await control.run(request, limit_seconds=timeout)
                if control and not control.reason
                else await asyncio.wait_for(request, timeout)
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            if 'rel="next"' in response.headers.get("link", ""):
                raise RemoteUnknown("remote_lookup_incomplete")
            return response.json()
        except (httpx.HTTPError, ValueError, TimeoutError, ExecutionStopped) as error:
            raise RemoteUnknown("remote_lookup_unavailable") from error

    async def stop_for_cancel(self) -> bool:
        if self.receipt.cancel_requested or (self.cancelled and await self.cancelled()):
            await self.save(
                phase="cancelled", cancel_requested=True, reason="publication_cancel_requested"
            )
            return True
        return False

    async def unknown(self, reason: str, **changes: Any) -> None:
        requested = self.receipt.cancel_requested or bool(self.cancelled and await self.cancelled())
        await self.save(phase="unknown", reason=reason, cancel_requested=requested, **changes)

    async def observe_base(self) -> bool:
        data = await self.get(f"git/ref/heads/{quote(self.intent.base_branch, safe='/')}")
        if not isinstance(data, dict) or not re.fullmatch(
            r"[0-9a-f]{40}",
            data.get("object", {}).get("sha", ""),
        ):
            raise RemoteUnknown("remote_base_unavailable")
        sha = data["object"]["sha"]
        observed = list(dict.fromkeys([*self.receipt.observed_base_shas, sha]))[-20:]
        moved = self.receipt.requires_revalidation or sha != self.intent.approved_base_sha
        await self.save(
            observed_base_shas=observed,
            requires_revalidation=moved,
            **(
                {"phase": "revalidation_required", "reason": "approved_base_moved"} if moved else {}
            ),
        )
        return not moved

    async def may_write(self) -> bool:
        if await self.stop_for_cancel():
            return False
        try:
            await self.store.authorize(self.intent)
        except (PublicationConflict, ApprovalConflict, GenerationConflict, SecurityError) as error:
            await self.save(
                phase="blocked", reason=getattr(error, "code", "publication_not_active")
            )
            return False
        return await self.observe_base()

    async def settle(self) -> bool:
        # Cancellation and base movement are independent facts. Finish the read-only
        # checks for a possibly sent request before stopping further writes.
        base_valid = await self.observe_base()
        if await self.stop_for_cancel():
            return False
        return base_valid

    async def step(
        self,
        name: str,
        lookup: Callable[[], Awaitable[dict | None]],
        path: str,
        payload: dict,
        *,
        unique_create: bool = True,
    ) -> bool:
        await self.save(step=name)
        found = await lookup()
        if found is None:
            if not unique_create and self.receipt.steps.get(name) in {
                "sending",
                "unknown",
                "confirmed",
            }:
                await self.unknown("remote_pr_outcome_unknown")
                return False
            if self.receipt.steps.get(name) == "confirmed":
                raise PublicationConflict("confirmed_remote_target_missing")
            if not await self.may_write():
                return False
            steps = {**self.receipt.steps, name: "sending"}
            await self.save(phase="publishing", steps=steps, reason=None)
            if await self.stop_for_cancel():
                steps.pop(name)
                await self.save(steps=steps)
                return False
            try:
                control = current_control()
                if control:
                    response = await control.run(
                        self.client.post(path, json=payload, timeout=control.timeout(30)),
                        limit_seconds=30,
                    )
                else:
                    response = await self.client.post(path, json=payload)
                response.raise_for_status()
                if name == "pr":
                    remote = self.validate_pr(response.json())
                    await self.save(remote_ids={**self.receipt.remote_ids, **remote})
            except (httpx.HTTPError, ValueError, TimeoutError, ExecutionStopped):
                await self.unknown(
                    "remote_request_outcome_unknown",
                    steps={**self.receipt.steps, name: "unknown"},
                )
            found = await lookup()
            if found is None:
                await self.unknown(
                    "remote_request_outcome_unknown",
                    steps={**self.receipt.steps, name: "unknown"},
                )
                return False
        await self.save(
            steps={**self.receipt.steps, name: "confirmed"},
            remote_ids={**self.receipt.remote_ids, **found},
            reason=None,
        )
        return await self.settle()

    async def blob(self, sha: str, content: bytes) -> dict | None:
        data = await self.get(f"git/blobs/{sha}")
        if data is None:
            return None
        try:
            decoded = base64.b64decode(data["content"].replace("\n", ""), validate=True)
            if data["sha"] != sha or data["encoding"] != "base64" or decoded != content:
                raise ValueError
        except (ValueError, KeyError, TypeError) as error:
            raise PublicationConflict("remote_blob_conflict") from error
        return {f"blob:{sha}": sha}

    async def tree(self, entries: list[dict]) -> dict | None:
        data = await self.get(f"git/trees/{self.intent.tree_sha}", params={"recursive": "1"})
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("truncated") is not False:
            raise RemoteUnknown("remote_tree_incomplete")
        expected = {entry["path"]: entry for entry in entries}
        directories = {
            "/".join(entry["path"].split("/")[:index])
            for entry in entries
            for index in range(1, len(entry["path"].split("/")))
        }
        actual, seen_dirs = {}, set()
        try:
            for entry in data["tree"]:
                path = entry["path"]
                if entry["type"] == "tree":
                    nested = [
                        {**item, "path": item["path"][len(path) + 1 :]}
                        for item in entries
                        if item["path"].startswith(path + "/")
                    ]
                    if (
                        path in seen_dirs
                        or entry["mode"] != "040000"
                        or (entry["sha"] != git_tree(nested))
                    ):
                        raise ValueError
                    seen_dirs.add(path)
                else:
                    if path in actual:
                        raise ValueError
                    actual[path] = {key: entry[key] for key in ("path", "mode", "type", "sha")}
            if (
                actual != expected
                or seen_dirs != directories
                or data["sha"] != self.intent.tree_sha
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationConflict("remote_tree_conflict") from error
        return {"tree_sha": self.intent.tree_sha}

    async def commit(self) -> dict | None:
        data = await self.get(f"git/commits/{self.intent.commit_sha}")
        if data is None:
            return None
        try:
            payload = commit_payload(self.intent)
            valid = (
                data["sha"] == self.intent.commit_sha
                and data["tree"]["sha"] == self.intent.tree_sha
                and [parent["sha"] for parent in data["parents"]] == [self.intent.approved_base_sha]
                and data["message"] == self.intent.message
            )
            for role in ("author", "committer"):
                valid = valid and all(
                    data[role][key] == payload[role][key] for key in ("name", "email")
                )
                valid = (
                    valid and datetime.fromisoformat(data[role]["date"]) == self.intent.commit_date
                )
            if not valid:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationConflict("remote_commit_conflict") from error
        return {"commit_sha": self.intent.commit_sha}

    async def ref(self) -> dict | None:
        data = await self.get(f"git/ref/heads/{quote(self.intent.branch, safe='/')}")
        if data is None:
            return None
        if not isinstance(data, dict) or (
            data.get("ref") != f"refs/heads/{self.intent.branch}"
            or data.get("object", {}).get("type") != "commit"
            or data.get("object", {}).get("sha") != self.intent.commit_sha
        ):
            raise PublicationConflict("remote_branch_conflict")
        return {"ref": f"refs/heads/{self.intent.branch}"}

    def validate_pr(self, data: dict) -> dict:
        try:
            number = data["number"]
            url = f"https://github.com/{self.intent.target_repo}/pull/{number}"
            if (
                type(number) is not int
                or number < 1
                or data["html_url"].lower() != url
                or data["head"]["ref"] != self.intent.branch
                or data["head"]["sha"] != self.intent.commit_sha
                or data["head"]["repo"]["full_name"].lower() != self.intent.target_repo
                or data["base"]["ref"] != self.intent.base_branch
                or data["base"]["repo"]["full_name"].lower() != self.intent.target_repo
                or data["body"] != self.intent.body
                or data["draft"] is not True
                or data["state"] != "open"
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise PublicationConflict("remote_pr_conflict") from error
        return {"pr_number": number, "pull_request_url": url}

    async def pr(self) -> dict | None:
        owner = self.intent.target_repo.split("/")[0]
        data = await self.get(
            "pulls",
            params={
                "head": f"{owner}:{self.intent.branch}",
                "state": "all",
                "per_page": 100,
            },
        )
        if not isinstance(data, list):
            raise RemoteUnknown("remote_pr_lookup_unavailable")
        if not data:
            return None
        if len(data) != 1:
            raise PublicationConflict("remote_pr_conflict")
        return self.validate_pr(data[0])

    async def run(self, entries: list[dict], blobs: dict[str, bytes]) -> None:
        try:

            async def tree_lookup():
                return await self.tree(entries)

            lookups = {"tree": tree_lookup, "commit": self.commit, "ref": self.ref, "pr": self.pr}
            for sha, content in blobs.items():

                async def blob_lookup(sha=sha, content=content):
                    return await self.blob(sha, content)

                lookups[f"blob:{sha}"] = blob_lookup
            # Reconcile all possibly sent requests first, even after cancellation/base movement.
            # Otherwise a late PR could be hidden by stopping at an earlier completed blob.
            for name, status in list(self.receipt.steps.items()):
                if name not in lookups:
                    raise PublicationConflict("corrupt_publication_step")
                found = await lookups[name]()
                if found is not None:
                    await self.save(
                        steps={**self.receipt.steps, name: "confirmed"},
                        remote_ids={**self.receipt.remote_ids, **found},
                    )
                elif status == "confirmed":
                    raise PublicationConflict("confirmed_remote_target_missing")
            if not self.receipt.steps and await self.stop_for_cancel():
                return
            if not await self.settle():
                return
            if self.reconcile_only:
                complete = all(self.receipt.steps.get(name) == "confirmed" for name in lookups)
                await self.save(
                    phase="published" if complete else "unknown",
                    reason=None if complete else "reconciliation_incomplete",
                )
                return
            for sha, content in sorted(blobs.items()):

                async def lookup(sha=sha, content=content):
                    return await self.blob(sha, content)

                if not await self.step(
                    f"blob:{sha}",
                    lookup,
                    "git/blobs",
                    {
                        "content": base64.b64encode(content).decode(),
                        "encoding": "base64",
                    },
                ):
                    return

            if not await self.step("tree", tree_lookup, "git/trees", {"tree": entries}):
                return
            if not await self.step(
                "commit", self.commit, "git/commits", commit_payload(self.intent)
            ):
                return
            if not await self.step(
                "ref",
                self.ref,
                "git/refs",
                {
                    "ref": f"refs/heads/{self.intent.branch}",
                    "sha": self.intent.commit_sha,
                },
            ):
                return
            if not await self.step(
                "pr",
                self.pr,
                "pulls",
                {
                    "title": self.intent.title,
                    "head": self.intent.branch,
                    "base": self.intent.base_branch,
                    "body": self.intent.body,
                    "draft": True,
                },
                unique_create=False,
            ):
                return
            await self.save(phase="published", step=None, reason=None)
        except RemoteUnknown as error:
            await self.unknown(str(error))
        except PublicationConflict as error:
            await self.save(phase="blocked", reason=error.code)
        except (KeyError, TypeError, AttributeError, ValueError):
            await self.unknown("remote_response_invalid")
