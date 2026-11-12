"""Update timing diagnostics: concise stages, handoff continuity, honest waiting.

The update already owns three seams: ``update_stage`` publishes desktop status,
``update_receipt.record_stage`` writes the durable stage marks, and
``_UpdateOutputStream`` tees terminal output into ``logs/update.log``. These
tests pin the behaviour added on top of those seams rather than a second
logging framework.
"""

from __future__ import annotations

import io
import re
from contextlib import contextmanager

import pytest


def _timing():
    from hermes_cli import update_timing

    return update_timing


class FakeClock:
    """Deterministic monotonic + wall pair; one second of wall time per tick."""

    def __init__(self) -> None:
        self.mono = 1000.0

    def monotonic(self) -> float:
        return self.mono

    def wall(self) -> float:
        return 1_760_000_000.0 + (self.mono - 1000.0)

    def advance(self, seconds: float) -> None:
        self.mono += seconds


@pytest.fixture
def clock():
    timing = _timing()
    fake = FakeClock()
    timing.configure(monotonic=fake.monotonic, wall=fake.wall, ticker=False)
    timing.start_run()
    yield fake
    timing.reset()


@contextmanager
def _receipt():
    from hermes_cli import update_receipt

    update_receipt.begin_update_receipt()
    try:
        yield update_receipt
    finally:
        update_receipt.finalize_update_receipt("success")


def _out(capsys) -> str:
    return capsys.readouterr().out


def test_default_output_is_timestamped_start_and_end_with_total(clock, capsys):
    timing = _timing()
    timing.begin_stage("apply")
    clock.advance(3.4)

    with _receipt() as receipt:
        receipt.record_stage("apply", "success", mode="git")
        mark = receipt._current.get().data["stages"][-1]

    out = _out(capsys)
    assert re.search(r"\[\d\d:\d\d:\d\d\] ▶ Fetching updates", out)
    assert re.search(r"\[\d\d:\d\d:\d\d\] ✓ Fetching updates — 3\.4s · total 3\.4s", out)
    assert mark["duration_ms"] == "3400"
    assert mark["started_at"].endswith("+00:00")


def test_each_end_line_carries_the_run_total(clock, capsys):
    timing = _timing()
    with _receipt() as receipt:
        timing.begin_stage("apply")
        clock.advance(3.4)
        receipt.record_stage("apply", "success")

        timing.begin_stage("snapshot")
        clock.advance(1.1)
        receipt.record_stage("snapshot", "success")

    out = _out(capsys)
    assert "Backing up state — 1.1s · total 4.5s" in out


def test_substeps_are_log_only_under_verbose(clock, capsys):
    timing = _timing()
    timing.begin_stage("deps")
    timing.substep("PM: resolving the dependency environment")

    quiet = _out(capsys)
    assert "PM: resolving" not in quiet

    timing.configure(verbose=True)
    timing.begin_stage("build")
    timing.substep("TUI build started")

    verbose = _out(capsys)
    assert re.search(r"\[\d\d:\d\d:\d\d\]   · TUI build started", verbose)


def test_handoff_continues_monotonic_total_and_active_stage(clock, capsys):
    timing = _timing()
    timing.begin_stage("deps")
    clock.advance(2.5)
    exported = timing.export_state()
    assert exported["active"]["name"] == "deps"
    assert exported["active"]["elapsed_s"] == 2.5

    timing.reset()
    timing.configure(monotonic=clock.monotonic, wall=clock.wall, ticker=False)
    timing.adopt_handoff({"verbose": False, "timing": exported})
    clock.advance(1.5)
    facts = timing.end_stage("deps", "success")

    assert facts["duration_ms"] == "4000"
    assert "Updating dependencies — 4.0s · total 4.0s" in _out(capsys)


def test_handoff_without_active_stage_keeps_parent_elapsed(clock, capsys):
    timing = _timing()
    timing.begin_stage("apply")
    clock.advance(2.0)
    timing.end_stage("apply", "success")
    exported = timing.export_state()

    timing.reset()
    timing.configure(monotonic=clock.monotonic, wall=clock.wall, ticker=False)
    timing.adopt_handoff({"timing": exported})
    timing.begin_stage("build")
    clock.advance(5.0)
    timing.end_stage("build", "success")

    assert "Building apps — 5.0s · total 7.0s" in _out(capsys)


def test_waiting_notice_appears_only_after_real_silence(clock, capsys):
    timing = _timing()
    timing.begin_stage("apply")

    clock.advance(19)
    assert timing.poll_waiting() is False
    clock.advance(2)
    assert timing.poll_waiting() is True
    assert "Fetching updates still running — 21.0s · no output for 21.0s" in _out(capsys)

    timing.note_output()
    clock.advance(10)
    assert timing.poll_waiting() is False


def test_waiting_notice_does_not_race_a_stage_that_just_ended(monkeypatch):
    """The ticker may poll while the main thread closes the stage: no exception, no stale notice."""
    timing = _timing()
    timing.configure(ticker=False)
    timing.start_run()
    timing.begin_stage("apply")
    calls = []

    def racing_monotonic():
        calls.append(1)
        if len(calls) == 1:
            timing.end_stage("apply", "success")  # the main thread's close lands mid-poll
        return 1000.0 + 25.0 * len(calls)

    monkeypatch.setattr(timing, "_monotonic", racing_monotonic)
    try:
        assert timing.poll_waiting() is False
    finally:
        timing.reset()


def test_fragmented_and_cr_progress_resets_the_silence_clock(clock):
    from hermes_cli.main_dashboard import _UpdateOutputStream

    timing = _timing()
    terminal, log = io.StringIO(), io.StringIO()
    stream = _UpdateOutputStream(terminal, log)
    timing.begin_stage("apply")
    clock.advance(25)

    stream.write("Receiving ob")
    stream.write("jects: 42%\r")
    stream.write("Receiving objects: 80%\r")

    assert log.getvalue() == "Receiving objects: 42%\rReceiving objects: 80%\r"
    assert terminal.getvalue() == log.getvalue()
    assert timing.poll_waiting() is False

    clock.advance(21)
    assert timing.poll_waiting() is True


def test_failure_closes_the_active_stage_on_the_existing_receipt(clock, capsys):
    timing = _timing()
    with _receipt() as receipt:
        timing.begin_stage("apply")
        clock.advance(4.2)
        timing.finish_run("failed")

        data = receipt._current.get().data
        marks = [mark for mark in data["stages"] if mark["name"] == "apply"]

    out = _out(capsys)
    assert "✗ Fetching updates — failed after 4.2s" in out
    assert marks and marks[-1]["outcome"] == "failed"
    assert marks[-1]["duration_ms"] == "4200"
    assert data["timing"]["outcome"] == "failed"
    assert data["timing"]["last_stage"]["name"] == "apply"


def test_interrupt_keeps_timing_without_claiming_a_failed_stage(clock, capsys):
    timing = _timing()
    with _receipt() as receipt:
        timing.begin_stage("deps")
        clock.advance(3.1)
        timing.finish_run("interrupted")
        data = receipt._current.get().data
        receipt.finalize_interrupted_update_receipt("ctrl-c")

    out = _out(capsys)
    assert "✗ Updating dependencies — interrupted after 3.1s" in out
    assert data["timing"]["outcome"] == "interrupted"
    assert all(mark.get("outcome") != "interrupted" for mark in data.get("stages", []))


def test_slow_summary_orders_steps_and_omits_fast_ones(clock, capsys):
    timing = _timing()
    for name, seconds in (("plan", 1.0), ("build", 5.0), ("restart", 3.0)):
        timing.begin_stage(name)
        clock.advance(seconds)
        timing.end_stage(name, "success")
    timing.finish_run("success")

    summary = [line for line in _out(capsys).splitlines() if "Total" in line][-1]
    assert "Building apps 5.0s" in summary
    assert "Restarting services 3.0s" in summary
    assert "Planning update" not in summary
    assert summary.index("Building apps") < summary.index("Restarting services")


def test_receipt_stage_marks_absorb_child_durations_for_the_final_summary(clock, capsys):
    timing = _timing()
    timing.absorb_receipt({"stages": [
        {"name": "apply", "outcome": "success", "duration_ms": "3400"},
        {"name": "deps", "outcome": "success", "duration_ms": "45000"},
    ]})
    timing.finish_run("success")

    summary = [line for line in _out(capsys).splitlines() if "Total" in line][-1]
    assert "Updating dependencies 45.0s" in summary
    assert "Fetching updates 3.4s" in summary


def test_verbose_substeps_never_echo_secrets(clock, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_SECRET", "sk-live-ENVSECRET")
    timing = _timing()
    timing.configure(verbose=True)
    timing.begin_stage("deps")
    timing.substep("Authorization: Bearer sk-live-ARGSSECRET")
    timing.substep("https://user:pass@example.com/repo")
    timing.substep("token=sk-live-TOKENSECRET")

    out = _out(capsys)
    assert "sk-live-ENVSECRET" not in out
    assert "sk-live-ARGSSECRET" not in out
    assert "sk-live-TOKENSECRET" not in out
    assert "user:pass@" not in out
    assert "***" in out


def test_update_parser_exposes_an_explicit_verbose_flag():
    import argparse

    from hermes_cli.subcommands.update import build_update_parser

    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda args: None)

    assert parser.parse_args(["update"]).verbose is False
    assert parser.parse_args(["update", "--verbose"]).verbose is True


def test_command_boundary_closes_timing_on_sys_exit(monkeypatch, capsys):
    from hermes_cli import update_cmd

    timing = _timing()
    timing.configure(ticker=False)

    def failing_body(args, gateway_mode):
        timing.start_run()
        timing.begin_stage("apply")
        raise SystemExit(1)

    monkeypatch.setattr(update_cmd, "_cmd_update_impl_body", failing_body)
    with _receipt() as receipt:
        with pytest.raises(SystemExit):
            update_cmd._cmd_update_impl(object(), False)
        data = receipt._current.get().data

    assert "✗ Fetching updates — failed after" in _out(capsys)
    assert data["timing"]["outcome"] == "failed"


def test_completion_request_carries_verbose_and_timing_mode():
    from hermes_cli import update_cmd

    opts = update_cmd._UpdateOptions(
        pre_update_version="1.0", gw_input_fn=None, assume_yes=True, keep_stash=False,
        switch_branch=False, discard_local_changes=False, verbose=True)

    request = update_cmd._source_completion_request(
        opts, None, None, None, desktop=False, gateway_mode=False)
    assert request["verbose"] is True
    update_cmd._completion_receipt.finalize_update_receipt("success")


def test_lost_completion_after_commit_is_settled_not_crashed(monkeypatch):
    """A child that dies without a terminal receipt must settle the owed tail (C3), not raise.

    ``run_completion`` maps an answer without a correlated receipt to non-zero; the parent then
    owes the completion follow-up instead of reporting a failed update.  This drives the existing
    caller with that lost result and asserts the settlement contract (request + reason) is reached.
    """
    from hermes_cli import update_cmd, update_completion

    settled = []

    def settle(request, reason):
        settled.append(reason)
        return {"exit_code": 0, "receipt": None, "windows_resume": None, "pm_receipt": None}

    monkeypatch.setattr(update_completion, "settle_lost_completion", settle)
    monkeypatch.setattr(update_cmd, "run_completion", lambda request: {
        "exit_code": 7, "error": "", "receipt": None, "windows_resume": None, "pm_receipt": None})
    monkeypatch.setattr(update_cmd, "_write_fleet_restart_pending_marker", lambda **kwargs: None)
    monkeypatch.setattr(update_cmd, "_accept_completion_pm_receipt", lambda *args: None)
    monkeypatch.setattr(update_cmd, "_unrestored_autostash_notice", lambda: None)

    with _receipt() as receipt:
        update_cmd._complete_source_update({
            "expected_sha": "a" * 40, "receipt": receipt._current.get().data,
            "windows_resume": None, "branch": "main", "source": "unused"})

    assert settled == ["the completion process exited 7"]


def test_git_failure_finishes_timing_before_the_receipt_is_finalized(monkeypatch, capsys):
    import subprocess

    from hermes_cli import update_cmd

    timing = _timing()
    timing.configure(ticker=False)
    monkeypatch.setattr(update_cmd, "_unrestored_autostash_notice", lambda: None)
    monkeypatch.setattr(update_cmd, "_should_zip_fallback_on_update_error", lambda exc: False)
    monkeypatch.setattr(update_cmd, "_print_called_process_error_tail", lambda exc: None)
    monkeypatch.setattr(update_cmd, "_finalize_receipt", lambda *args: None)

    with _receipt() as receipt:
        timing.start_run()
        timing.begin_stage("apply")
        with pytest.raises(SystemExit):
            update_cmd._handle_update_called_process_error(
                subprocess.CalledProcessError(1, ["git", "fetch"], stderr=""), object(), False, False)
        data = receipt._current.get().data

    assert data["timing"]["outcome"] == "failed"
    assert "✗ Fetching updates — failed after" in _out(capsys)
