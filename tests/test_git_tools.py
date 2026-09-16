"""Local git: the half of "do the fix, raise a PR" that happens on this machine.

Driven against real repositories in tmp_path rather than mocks. git's own
behaviour is the thing being relied on --- what `switch -c` does to
uncommitted changes, what `diff --staged` shows --- and a mock would be
asserting my belief about git rather than git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from altus.cloud.base import ProtectionRules, Sensitivity
from altus.tools.approval import Decision, RecordingPolicy
from altus.tools.base import CloudContext, ToolContext
from altus.tools.git import (
    GitBranchTool,
    GitCommitTool,
    GitDiffTool,
    GitLogTool,
    GitPushTool,
    GitStatusTool,
)
from altus.workspace import Workspace


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "test@example.invalid", cwd=root)
    git("config", "user.name", "Test", cwd=root)
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "first", cwd=root)
    return root


def context(repo: Path, policy=None, rules=None) -> ToolContext:  # type: ignore[no-untyped-def]
    return ToolContext(
        workspace=Workspace(root=repo),
        approvals=policy or RecordingPolicy(),
        cloud=CloudContext(protection=rules),
    )


# ------------------------------------------------------------------- reads


async def test_status_reports_the_branch_and_the_changes(repo: Path) -> None:
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    out = await GitStatusTool().run({}, context(repo))
    assert not out.is_error
    assert "main" in out.content
    assert "app.py" in out.content and "new.txt" in out.content


async def test_the_reads_say_so_outside_a_repository(tmp_path: Path) -> None:
    """A clear refusal, not a confusing git error --- the workflow that hits
    this is running in the wrong directory."""
    plain = tmp_path / "plain"
    plain.mkdir()
    for tool in (GitStatusTool(), GitLogTool(), GitDiffTool()):
        out = await tool.run({}, context(plain))
        assert out.is_error and "not inside a git repository" in out.content


async def test_log_is_capped_however_much_is_asked_for(repo: Path) -> None:
    out = await GitLogTool().run({"limit": 10_000}, context(repo))
    assert not out.is_error
    assert len(out.content.splitlines()) <= 200


async def test_a_path_outside_the_workspace_is_refused(repo: Path) -> None:
    """Resolved through the workspace, so the sandbox still decides what is
    reachable --- git is not a way around it."""
    from altus.core.errors import PathNotAllowed

    with pytest.raises(PathNotAllowed):
        await GitLogTool().run({"path": "../../etc/passwd"}, context(repo))


# --------------------------------------------------------------- mutations


async def test_a_branch_is_created_after_asking(repo: Path) -> None:
    policy = RecordingPolicy()
    out = await GitBranchTool().run({"name": "fix/thing"}, context(repo, policy))
    assert not out.is_error
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip() == "fix/thing"
    (request,) = policy.seen
    assert "fix/thing" in request.path
    assert "git switch main goes back" in request.recoverability


async def test_a_declined_branch_changes_nothing(repo: Path) -> None:
    policy = RecordingPolicy(decision=Decision.DENY)
    out = await GitBranchTool().run({"name": "fix/thing"}, context(repo, policy))
    assert out.denied
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip() == "main"


async def test_a_commit_stages_only_what_it_was_given(repo: Path) -> None:
    """`paths` is required rather than defaulting to everything: committing
    whatever happened to be in the tree is how unrelated work ends up in
    somebody's fix."""
    (repo / "app.py").write_text("print('fixed')\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("not mine\n", encoding="utf-8")

    policy = RecordingPolicy()
    out = await GitCommitTool().run(
        {"message": "fix the thing", "paths": ["app.py"]}, context(repo, policy)
    )
    assert not out.is_error
    assert "app.py" in git("show", "--stat", "--oneline", cwd=repo)
    assert "unrelated.txt" in git("status", "--porcelain", cwd=repo), "still uncommitted"


async def test_a_commit_shows_the_staged_diff_before_asking(repo: Path) -> None:
    (repo / "app.py").write_text("print('fixed')\n", encoding="utf-8")
    policy = RecordingPolicy()
    await GitCommitTool().run({"message": "fix", "paths": ["app.py"]}, context(repo, policy))
    (request,) = policy.seen
    assert "print('fixed')" in request.diff
    assert "main" in request.dry_run


async def test_a_commit_with_no_paths_is_refused(repo: Path) -> None:
    out = await GitCommitTool().run({"message": "x", "paths": []}, context(repo))
    assert out.is_error
    assert "unrelated work" in out.content


async def test_a_declined_commit_says_what_it_left_staged(repo: Path) -> None:
    """Unstaging would throw away whatever the user had already staged
    themselves, so it is left alone and said out loud."""
    (repo / "app.py").write_text("print('fixed')\n", encoding="utf-8")
    policy = RecordingPolicy(decision=Decision.DENY)
    out = await GitCommitTool().run({"message": "fix", "paths": ["app.py"]}, context(repo, policy))
    assert out.denied and "still staged" in out.content
    assert git("log", "--oneline", cwd=repo).count("\n") == 1


# ------------------------------------------------------------------- push


def with_remote(repo: Path, tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    git("remote", "add", "origin", str(bare), cwd=repo)
    return bare


async def test_pushing_a_protected_branch_needs_the_name_typed(repo: Path, tmp_path: Path) -> None:
    """`main` is protected by convention, not only by configuration --- a
    default that has to be configured to work protects nobody on day one."""
    with_remote(repo, tmp_path)
    policy = RecordingPolicy()
    await GitPushTool().run({"set_upstream": True}, context(repo, policy))

    (request,) = policy.seen
    assert request.protected
    assert request.needs_challenge
    assert request.target == "git: origin · main"


async def test_an_ordinary_branch_does_not(repo: Path, tmp_path: Path) -> None:
    """The other half. Challenging every push is how a challenge stops
    being read."""
    with_remote(repo, tmp_path)
    git("switch", "-q", "-c", "fix/thing", cwd=repo)
    policy = RecordingPolicy()
    await GitPushTool().run({"set_upstream": True}, context(repo, policy))

    (request,) = policy.seen
    assert not request.protected
    assert request.sensitivity is Sensitivity.MUTATE


async def test_a_branch_matching_a_protection_pattern_is_protected(
    repo: Path, tmp_path: Path
) -> None:
    """The same `*prod*` rules that guard a cluster, with no second list of
    protected things to keep in step."""
    with_remote(repo, tmp_path)
    git("switch", "-q", "-c", "release/prod-eu", cwd=repo)
    policy = RecordingPolicy()
    rules = ProtectionRules.build(["*prod*"], [], "confirm")
    await GitPushTool().run({}, context(repo, policy, rules))

    (request,) = policy.seen
    assert request.protected


async def test_the_push_prompt_does_not_claim_a_dry_run(repo: Path, tmp_path: Path) -> None:
    with_remote(repo, tmp_path)
    git("switch", "-q", "-c", "fix/thing", cwd=repo)
    policy = RecordingPolicy()
    await GitPushTool().run({}, context(repo, policy))

    (request,) = policy.seen
    assert "Not a dry run" in request.dry_run
    assert "force-push" in request.recoverability


async def test_a_declined_push_pushes_nothing(repo: Path, tmp_path: Path) -> None:
    bare = with_remote(repo, tmp_path)
    git("switch", "-q", "-c", "fix/thing", cwd=repo)
    out = await GitPushTool().run(
        {"set_upstream": True}, context(repo, RecordingPolicy(decision=Decision.DENY))
    )
    assert out.denied
    assert not git("branch", "--list", cwd=bare).strip(), "the remote has no branches"


async def test_pushing_with_no_remote_says_so(repo: Path) -> None:
    out = await GitPushTool().run({}, context(repo))
    assert out.is_error and "no remote" in out.content
