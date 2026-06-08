"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(cwd=None, skip_soul=False):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestSkillsIndexEmittedLast:
    def test_skills_marker_after_static_blocks(self, monkeypatch):
        # The mutable skills index must be the LAST block in the stable tier so
        # a skill mutation doesn't cache-invalidate the static blocks that used
        # to follow it (alibaba workaround, environment hints, Python probe,
        # active-profile hint, platform hints).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        agent = _make_agent(
            valid_tool_names=["skills_list"],
            platform="cli",  # triggers a platform-hint block
        )

        env_marker = "ENV-HINT-MARKER"
        skills_marker = "<available_skills>SKILL</available_skills>"

        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=env_marker),
            patch("run_agent.build_context_files_prompt", return_value=""),
            patch("run_agent.get_toolset_for_tool", return_value=None),
            patch("run_agent.build_skills_system_prompt", return_value=skills_marker),
        ):
            parts = build_system_prompt_parts(agent)

        stable = parts["stable"]
        assert "<available_skills>" in stable
        assert env_marker in stable
        # Environment hint, active-profile hint, and platform hint must all
        # precede the skills index marker.
        assert stable.index(env_marker) < stable.index("<available_skills>")
        assert stable.index("Active Hermes profile:") < stable.index("<available_skills>")


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path
