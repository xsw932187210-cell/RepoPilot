"""Store fixtures and an observable GitHub protocol server. Never uses GitHub."""

from __future__ import annotations

import base64
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from approval_helpers import approval_case, decision_request

from repopilot.approvals import ApprovalStore
from repopilot.candidates import ArtifactStore
from repopilot.db import Database, TaskRecord
from repopilot.models import TaskStatus
from repopilot.publication import PublicationStore, git_oid, git_tree


async def publication_case(database: Database, root: Path):
    settings, approval = await approval_case(database, root)
    settings.github_write_enabled = True
    settings.github_token = "fixture-credential"  # noqa: S105 -- local test transport only.
    settings.github_allowed_owners = "example"
    async with database.sessions() as session, session.begin():
        task = await session.get(TaskRecord, approval.task_id)
        task.repository_url = "https://github.com/example/project"
    approvals = ApprovalStore(database)
    decision = await approvals.decide(
        approval.task_id, decision_request(approval), actor="operator"
    )
    queued = await database.get_task(approval.task_id)
    running = await database.transition_task(
        queued.id,
        expected_status=queued.status,
        expected_version=queued.state_version,
        status=TaskStatus.RUNNING,
    )
    resume = await approvals.resume_for_running_task(running)
    await approvals.validate_resume(running.id, resume)
    store = PublicationStore(database, ArtifactStore(root / ".artifacts"))
    intent = await store.ensure(running.id, decision.decision_id, approval.candidate.candidate_id)
    return settings, store, intent, approval


class GitHubServer:
    def __init__(self, base_sha: str, state_file: Path | None = None):
        self.state_file = state_file
        self.state: dict[str, Any] = {
            "base": base_sha,
            "blobs": {},
            "trees": {},
            "commits": {},
            "refs": {},
            "pulls": [],
            "post_counts": {},
        }
        if state_file and state_file.exists():
            self.state = json.loads(state_file.read_text())
        self.requests: list[httpx.Request] = []
        self.posts: Counter[str] = Counter(self.state.get("post_counts", {}))
        self.lose: str | None = None
        self.unavailable = False
        self.hide_pr = False
        self.after_write = None
        self.after_get = None
        self.truncate_tree = False

    def persist(self):
        if self.state_file:
            self.state_file.write_text(json.dumps(self.state, sort_keys=True))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/repos/example/project/")
        if request.method == "GET":
            if self.unavailable:
                return httpx.Response(503, json={"message": "unavailable"})
            if path == "git/ref/heads/main":
                result = {
                    "ref": "refs/heads/main",
                    "object": {
                        "type": "commit",
                        "sha": self.state["base"],
                    },
                }
            elif path.startswith("git/ref/heads/"):
                branch = path.removeprefix("git/ref/heads/")
                result = self.state["refs"].get(branch)
            elif path.startswith("git/blobs/"):
                result = self.state["blobs"].get(path.rsplit("/", 1)[1])
            elif path.startswith("git/trees/"):
                result = self.state["trees"].get(path.rsplit("/", 1)[1])
                if result and self.truncate_tree:
                    result = {**result, "truncated": True}
            elif path.startswith("git/commits/"):
                result = self.state["commits"].get(path.rsplit("/", 1)[1])
            elif path == "pulls":
                assert request.url.params["state"] == "all"
                assert request.url.params["head"].startswith("example:repopilot/")
                result = [] if self.hide_pr else self.state["pulls"]
            else:
                raise AssertionError(f"Unexpected GET {path}")
            if self.after_get:
                await self.after_get(path, result)
            return httpx.Response(404 if result is None else 200, json=result)
        assert request.method == "POST", "No updates, deletes or merges are permitted"
        payload = json.loads(request.content)
        if path == "git/blobs":
            stage = "blob"
            content = base64.b64decode(payload["content"])
            sha = git_oid("blob", content)
            result = {"sha": sha, "encoding": "base64", "content": payload["content"]}
            self.state["blobs"][sha] = result
        elif path == "git/trees":
            stage = "tree"
            assert "base_tree" not in payload, "The full approved tree is authoritative"
            entries = payload["tree"]
            sha = git_tree(entries)
            directories = {
                "/".join(item["path"].split("/")[:index])
                for item in entries
                for index in range(1, len(item["path"].split("/")))
            }
            nested = [
                {
                    "path": path,
                    "mode": "040000",
                    "type": "tree",
                    "sha": git_tree(
                        [
                            {**item, "path": item["path"][len(path) + 1 :]}
                            for item in entries
                            if item["path"].startswith(path + "/")
                        ]
                    ),
                }
                for path in sorted(directories)
            ]
            result = {"sha": sha, "tree": entries + nested, "truncated": False}
            self.state["trees"][sha] = result
        elif path == "git/commits":
            stage = "commit"
            people = []
            for role in ("author", "committer"):
                person = payload[role]
                stamp = int(datetime.fromisoformat(person["date"]).timestamp())
                people.append(f"{role} {person['name']} <{person['email']}> {stamp} +0000\n")
            raw = (
                f"tree {payload['tree']}\n"
                + "".join(f"parent {parent}\n" for parent in payload["parents"])
                + "".join(people)
                + "\n"
                + payload["message"]
            ).encode()
            sha = git_oid("commit", raw)
            result = {
                **payload,
                "sha": sha,
                "tree": {"sha": payload["tree"]},
                "parents": [{"sha": parent} for parent in payload["parents"]],
            }
            self.state["commits"][sha] = result
        elif path == "git/refs":
            stage = "ref"
            branch = payload["ref"].removeprefix("refs/heads/")
            if branch in self.state["refs"]:
                return httpx.Response(422, json={"message": "already exists"})
            result = {"ref": payload["ref"], "object": {"type": "commit", "sha": payload["sha"]}}
            self.state["refs"][branch] = result
        elif path == "pulls":
            stage = "pr"
            assert payload["draft"] is True
            number = len(self.state["pulls"]) + 1
            result = {
                **payload,
                "number": number,
                "html_url": f"https://github.com/example/project/pull/{number}",
                "head": {
                    "ref": payload["head"],
                    "sha": self.state["refs"][payload["head"]]["object"]["sha"],
                    "repo": {"full_name": "example/project"},
                },
                "base": {
                    "ref": payload["base"],
                    "sha": self.state["base"],
                    "repo": {"full_name": "example/project"},
                },
                "state": "open",
            }
            self.state["pulls"].append(result)
        else:
            raise AssertionError(f"Unexpected POST {path}")
        self.posts[stage] += 1
        self.state["post_counts"] = dict(self.posts)
        self.persist()
        if self.after_write:
            await self.after_write(stage)
        if self.lose == stage:
            self.lose = None
            self.unavailable = True
            raise httpx.ReadTimeout("Response lost after application", request=request)
        return httpx.Response(201, json=result)
