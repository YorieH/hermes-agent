"""Tests: kanban worker spawn pins TERMINAL_CWD to the task workspace.

Regression coverage for #34619 and #41312 (same root cause): ``_default_spawn``
launched the worker subprocess with ``cwd=workspace`` and set
``HERMES_KANBAN_WORKSPACE``, but did NOT set ``TERMINAL_CWD``. Because
``TERMINAL_CWD`` takes precedence over the process cwd in both
``tools/file_tools.py::_resolve_base_dir`` (relative ``write_file`` paths) and
``agent_init``'s context-file loader (``AGENTS.md`` discovery), workers inherited
the dispatching gateway's cwd — relative writes landed in the gateway user's
home (#41312) and the wrong profile's ``AGENTS.md`` was loaded (#34619).
Pinning ``TERMINAL_CWD`` to the workspace fixes both.
"""

from __future__ import annotations

import subprocess


def _make_task(kb, *, assignee: str = "w"):
    return kb.Task(
        id="t_cwd",
        title="cwd pin",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
    )


def _capture_spawn_env(kb, monkeypatch, workspace: str) -> dict:
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kb._default_spawn(_make_task(kb), workspace)
    return captured


def test_terminal_cwd_pinned_to_workspace(monkeypatch, tmp_path):
    """A real, absolute workspace dir is pinned as TERMINAL_CWD."""
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn_env(kb, monkeypatch, str(workspace))

    assert captured["env"]["TERMINAL_CWD"] == str(workspace)
    # The subprocess cwd and TERMINAL_CWD must agree — both anchor the workspace.
    assert captured["cwd"] == str(workspace)
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(workspace)


def test_terminal_cwd_not_pinned_for_nonexistent_workspace(monkeypatch, tmp_path):
    """A non-directory workspace must NOT clobber the inherited TERMINAL_CWD.

    file_tools rejects relative / sentinel TERMINAL_CWD values, so writing a
    meaningless (nonexistent) path would be worse than leaving the inherited
    one. The guard requires an existing absolute dir.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("TERMINAL_CWD", "/pre/existing/anchor")

    from hermes_cli import kanban_db as kb

    missing = tmp_path / "does-not-exist"

    captured = _capture_spawn_env(kb, monkeypatch, str(missing))

    # Inherited value is preserved (not overwritten with a bogus path).
    assert captured["env"]["TERMINAL_CWD"] == "/pre/existing/anchor"


def test_default_spawn_propagates_task_notification_origin(monkeypatch, tmp_path):
    """Child cards created by a worker inherit the parent task's notifier."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "stale")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "wrong")
    monkeypatch.setenv("HERMES_KANBAN_NOTIFIER_PROFILE", "wrong-owner")

    from hermes_cli import kanban_db as kb

    kb.init_db()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="parent",
            assignee="w",
            session_id="session-parent",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="haru-chat",
            thread_id="haru-thread",
            user_id="haru-user",
            notifier_profile="kurumi",
        )
        task = kb.get_task(conn, tid)

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    kb._default_spawn(task, str(workspace))

    env = captured["env"]
    assert env["HERMES_SESSION_ID"] == "session-parent"
    assert env["HERMES_SESSION_PLATFORM"] == "telegram"
    assert env["HERMES_SESSION_CHAT_ID"] == "haru-chat"
    assert env["HERMES_SESSION_THREAD_ID"] == "haru-thread"
    assert env["HERMES_SESSION_USER_ID"] == "haru-user"
    assert env["HERMES_KANBAN_NOTIFIER_PROFILE"] == "kurumi"


def test_default_spawn_clears_stale_session_env_without_subscription(monkeypatch, tmp_path):
    """A worker with no subscription must not inherit stale gateway routing."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "w"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "stale-chat")
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-session")
    monkeypatch.setenv("HERMES_SESSION_PROFILE", "stale-profile")
    monkeypatch.setenv("HERMES_KANBAN_NOTIFIER_PROFILE", "stale-owner")

    from hermes_cli import kanban_db as kb

    kb.init_db()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="parent", assignee="w")
        task = kb.get_task(conn, tid)

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    kb._default_spawn(task, str(workspace))

    env = captured["env"]
    assert "HERMES_SESSION_PLATFORM" not in env
    assert "HERMES_SESSION_CHAT_ID" not in env
    assert "HERMES_SESSION_ID" not in env
    assert "HERMES_SESSION_PROFILE" not in env
    assert "HERMES_KANBAN_NOTIFIER_PROFILE" not in env
