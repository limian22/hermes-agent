"""Concise, receipt-correlated timing for ``hermes update``.

The updater already has one output tee (``_UpdateOutputStream``), one durable
stage ledger (``update_receipt.record_stage``) and one desktop progress channel
(``update_stage.publish_stage``). This module adds timing to those seams only:
a timestamped start/end line per main stage, monotonic elapsed plus total, an
honest silence notice, verbose substeps, and a final slow-step summary.

Cross-process continuity matters because the post-commit tail runs in a
completion child. ``export_state`` / ``adopt_handoff`` carry the parent's
monotonic elapsed through the existing JSON request, so the child's total does
not restart at zero. No second log file, receipt, or environment variable.
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
import time
from typing import Any

#: Main stages. Tokens match the receipt/metrics contract where one exists.
STAGE_LABELS = {
    "plan": "Planning update",
    "snapshot": "Backing up state",
    "apply": "Fetching updates",
    "deps": "Updating dependencies",
    "build": "Building apps",
    "restart": "Restarting services",
    "verify": "Verifying update",
}

SLOW_STEP_SECONDS = 2.0
SILENCE_SECONDS = 20.0
WAIT_REPEAT_SECONDS = 60.0

_verbose = False
_monotonic = time.monotonic
_wall = time.time
_ticker_enabled = True

_run_started: float | None = None
_run_started_wall: float | None = None
_inherited_total = 0.0
_active: dict[str, Any] | None = None
_completed: dict[str, dict[str, str]] = {}
_slow: dict[str, float] = {}
_last_stage: dict[str, Any] | None = None
_last_output: float | None = None
_last_wait_notice: float | None = None
_finished = False
_summary_printed = False
_ticker_stop: threading.Event | None = None
_ticker_thread: threading.Thread | None = None


def configure(*, verbose: bool | None = None, monotonic=None, wall=None,
              ticker: bool | None = None) -> None:
    """Select verbosity/clocks/ticker. Production only passes ``verbose``."""
    global _verbose, _monotonic, _wall, _ticker_enabled
    if verbose is not None:
        _verbose = bool(verbose)
    if monotonic is not None:
        _monotonic = monotonic
    if wall is not None:
        _wall = wall
    if ticker is not None:
        _ticker_enabled = bool(ticker)


def reset() -> None:
    """Drop all run state (tests and a fresh command in one process)."""
    global _verbose, _monotonic, _wall, _ticker_enabled
    global _run_started, _run_started_wall, _inherited_total, _active
    global _completed, _slow, _last_stage, _last_output, _last_wait_notice
    global _finished, _summary_printed
    _stop_ticker()
    _verbose = False
    _monotonic = time.monotonic
    _wall = time.time
    _ticker_enabled = True
    _run_started = _run_started_wall = None
    _inherited_total = 0.0
    _active = None
    _completed = {}
    _slow = {}
    _last_stage = None
    _last_output = _last_wait_notice = None
    _finished = _summary_printed = False


def start_run() -> None:
    """Begin this process's clock; safe to call once at the command boundary."""
    global _run_started, _run_started_wall, _inherited_total, _active
    global _completed, _slow, _last_stage, _last_output, _last_wait_notice
    global _finished, _summary_printed
    _stop_ticker()
    _run_started = _monotonic()
    _run_started_wall = _wall()
    _inherited_total = 0.0
    _active = None
    _completed = {}
    _slow = {}
    _last_stage = None
    _last_output = _run_started
    _last_wait_notice = None
    _finished = _summary_printed = False


def begin_stage(name: str, detail: str = "") -> None:
    """Print the timestamped stage start and make it the active timer."""
    global _active, _last_output, _last_wait_notice
    if _run_started is None or _finished:
        return
    if _active is not None:
        if _active["name"] == name:
            return
        # A missing end mark must not hide the previous stage's time.
        end_stage(_active["name"], "skipped")
    now = _monotonic()
    _active = {
        "name": name,
        "label": STAGE_LABELS.get(name, name),
        "detail": str(detail),
        "started_mono": now,
        "started_wall": _wall(),
        "inherited_s": 0.0,
    }
    _last_output = now
    _last_wait_notice = None
    _emit(f"[{_stamp()}] ▶ {_active['label']}")
    _publish(_active["label"])
    _ensure_ticker()


def end_stage(name: str, outcome: str = "success") -> dict[str, str]:
    """Close the active stage, print its elapsed/total and return receipt facts."""
    global _active, _last_stage
    if _active is None or _active["name"] != name:
        return _completed.pop(name, {})
    elapsed = _active["inherited_s"] + max(0.0, _monotonic() - _active["started_mono"])
    facts = {
        "started_at": _utc_iso(_active["started_wall"]),
        "duration_ms": str(round(elapsed * 1000)),
    }
    _completed[name] = facts
    _slow[_active["label"]] = max(_slow.get(_active["label"], 0.0), elapsed)
    _last_stage = {"name": name, "outcome": outcome, "elapsed_s": round(elapsed, 3)}
    _emit(_end_line(_active["label"], outcome, elapsed, _total_seconds()))
    _active = None
    return facts


def stage_end_facts(name: str, outcome: str) -> dict[str, str]:
    """Timing facts for the existing receipt stage marker (never raises)."""
    try:
        if _active is not None and _active["name"] == name:
            return end_stage(name, outcome)
        return _completed.pop(name, {})
    except Exception:  # health: allow BLE001 -- timing must never break an update
        return {}


def substep(text: str) -> None:
    """A verbose-only detail line; the existing output tee puts it in update.log."""
    if not _verbose or _run_started is None or _finished:
        return
    clean = redact(str(text)).strip()
    if not clean:
        return
    _emit(f"[{_stamp()}]   · {clean}")
    note_output()


def note_output() -> None:
    """Any real output (including a CR progress redraw) resets the silence clock."""
    global _last_output
    if _run_started is not None and not _finished:
        _last_output = _monotonic()


def poll_waiting(now: float | None = None) -> bool:
    """Emit one honest silence notice when the active stage has gone quiet."""
    global _last_wait_notice
    # Read the stage once: the ticker races begin_stage/end_stage on the main thread, and a
    # second lookup after the clock call could find None (or a different stage).
    active = _active
    if active is None or _run_started is None or _finished:
        return False
    current = _monotonic() if now is None else now
    if _active is not active or _finished:
        return False
    elapsed = active["inherited_s"] + max(0.0, current - active["started_mono"])
    silent = current - (_last_output if _last_output is not None else current)
    if elapsed < SILENCE_SECONDS or silent < SILENCE_SECONDS:
        return False
    if _last_wait_notice is not None and current - _last_wait_notice < WAIT_REPEAT_SECONDS:
        return False
    _emit(f"[{_stamp()}] … {active['label']} still running — {_fmt(elapsed)}"
          f" · no output for {_fmt(silent)}")
    _last_wait_notice = current
    return True


def export_state() -> dict[str, Any]:
    """JSON-safe handoff state: monotonic total, active stage, slow steps."""
    active = None
    if _active is not None:
        active = {
            "name": _active["name"],
            "label": _active["label"],
            "detail": _active["detail"],
            "elapsed_s": round(
                _active["inherited_s"] + max(0.0, _monotonic() - _active["started_mono"]), 3),
        }
    return {
        "elapsed_s": round(_total_seconds(), 3),
        "active": active,
        "last_stage": _last_stage,
        "slow_steps": _slow_payload(),
    }


def adopt_handoff(request: dict[str, Any] | None) -> None:
    """Continue a parent's clock inside the completion child (no duplicate start)."""
    global _inherited_total, _active, _last_output, _last_wait_notice
    if not isinstance(request, dict):
        return
    configure(verbose=bool(request.get("verbose")))
    timing = request.get("timing")
    start_run()
    if not isinstance(timing, dict):
        return
    try:
        _inherited_total = max(0.0, float(timing.get("elapsed_s") or 0.0))
    except (TypeError, ValueError):
        _inherited_total = 0.0
    for step in timing.get("slow_steps") or ():
        if not isinstance(step, dict):
            continue
        try:
            seconds = float(step.get("seconds") or 0.0)
        except (TypeError, ValueError):
            continue
        label = str(step.get("label") or "")
        if label and seconds >= 0:
            _slow[label] = max(_slow.get(label, 0.0), seconds)
    active = timing.get("active")
    if isinstance(active, dict) and active.get("name"):
        try:
            inherited = max(0.0, float(active.get("elapsed_s") or 0.0))
        except (TypeError, ValueError):
            inherited = 0.0
        _active = {
            "name": str(active["name"]),
            "label": STAGE_LABELS.get(str(active["name"]), str(active.get("label") or active["name"])),
            "detail": str(active.get("detail") or ""),
            "started_mono": _monotonic(),
            "started_wall": _wall(),
            "inherited_s": inherited,
        }
        _last_output = _monotonic()
        _last_wait_notice = None
        _ensure_ticker()


def absorb_receipt(receipt: dict[str, Any] | None) -> None:
    """Add a completed child run's stage durations to the final summary."""
    if not isinstance(receipt, dict):
        return
    previous = receipt.get("started_at")
    for mark in receipt.get("stages") or ():
        if not isinstance(mark, dict):
            continue
        name = mark.get("name")
        if not isinstance(name, str) or name not in STAGE_LABELS:
            continue
        seconds = None
        try:
            if mark.get("duration_ms") is not None:
                seconds = float(mark["duration_ms"]) / 1000.0
            else:
                seconds = _elapsed_between(previous, mark.get("at"))
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None and seconds >= 0:
            label = STAGE_LABELS[name]
            _slow[label] = max(_slow.get(label, 0.0), seconds)
        previous = mark.get("at") or previous


def finish_run(outcome: str = "success", *, summary: bool = True) -> dict[str, Any] | None:
    """Close any open stage, persist timing on the receipt, print the summary."""
    global _finished, _summary_printed
    if _run_started is None or _finished:
        return None
    active_name = _active["name"] if _active is not None else None
    display_outcome = outcome
    if active_name is not None:
        if outcome == "interrupted":
            display_outcome = "interrupted"
        facts = end_stage(active_name, display_outcome)
        if outcome in {"failed", "interrupted"} and facts:
            # A failed stage is a real receipt boundary; interrupted is a run outcome.
            _completed.pop(active_name, None)
        if outcome == "failed" and facts:
            _record_failed_stage(active_name, facts)
    snapshot = export_state()
    snapshot["outcome"] = outcome
    _record_timing(snapshot)
    if summary and not _summary_printed:
        _print_summary(snapshot)
        _summary_printed = True
    _finished = True
    _stop_ticker()
    return snapshot


def stop() -> None:
    """Stop the waiting ticker without clearing the run (command-boundary cleanup)."""
    _stop_ticker()


def redact(text: str) -> str:
    """Remove credential-shaped text before a verbose detail line can reach the log."""
    clean = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1***@", str(text))
    clean = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 ***", clean)
    clean = re.sub(
        r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*=\s*\S+", r"\1=***", clean)
    clean = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "***", clean)
    return clean


def _record_failed_stage(name: str, facts: dict[str, str]) -> None:
    try:
        from hermes_cli import update_receipt

        update_receipt.record_stage(name, "failed", **facts)
    except Exception:  # health: allow BLE001 S110 -- the receipt must never break an update
        pass


def _record_timing(snapshot: dict[str, Any]) -> None:
    try:
        from hermes_cli import update_receipt

        current = update_receipt._current.get()
        if current is not None and not current.data.get("finished_at"):
            update_receipt.record_fact("timing", snapshot)
    except Exception:  # health: allow BLE001 S110 -- the receipt must never break an update
        pass


def _print_summary(snapshot: dict[str, Any]) -> None:
    slow = [step for step in snapshot["slow_steps"] if step["seconds"] >= SLOW_STEP_SECONDS][:5]
    line = f"[{_stamp()}] Total {_fmt(float(snapshot['elapsed_s']))}"
    if slow:
        line += " · slowest: " + ", ".join(
            f"{step['label']} {_fmt(float(step['seconds']))}" for step in slow)
    else:
        line += " · no slow steps (>= 2s)"
    _emit(line)


def _slow_payload() -> list[dict[str, Any]]:
    return [
        {"label": label, "seconds": round(seconds, 3)}
        for label, seconds in sorted(_slow.items(), key=lambda item: -item[1])
    ]


def _total_seconds() -> float:
    if _run_started is None:
        return _inherited_total
    return _inherited_total + max(0.0, _monotonic() - _run_started)


def _end_line(label: str, outcome: str, elapsed: float, total: float) -> str:
    if outcome == "failed":
        head = f"✗ {label} — failed after {_fmt(elapsed)}"
    elif outcome == "interrupted":
        head = f"✗ {label} — interrupted after {_fmt(elapsed)}"
    elif outcome == "skipped":
        head = f"⏭ {label} — skipped after {_fmt(elapsed)}"
    else:
        head = f"✓ {label} — {_fmt(elapsed)}"
    return f"[{_stamp()}] {head} · total {_fmt(total)}"


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{round(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def _stamp() -> str:
    return time.strftime("%H:%M:%S", time.localtime(_wall()))


def _utc_iso(wall: float) -> str:
    return _dt.datetime.fromtimestamp(wall, _dt.UTC).isoformat(timespec="seconds")


def _elapsed_between(start: Any, end: Any) -> float | None:
    try:
        first = _dt.datetime.fromisoformat(str(start))
        last = _dt.datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return max(0.0, (last - first).total_seconds())


def _emit(line: str) -> None:
    try:
        print(line, flush=True)
    except (OSError, ValueError):  # a vanished terminal is the output wrapper's problem
        pass


def _publish(label: str) -> None:
    try:
        from hermes_cli.update_stage import publish_stage

        publish_stage(label)
    except Exception:  # health: allow BLE001 S110 -- a UI hiccup never fails an update
        pass


def _ensure_ticker() -> None:
    global _ticker_stop, _ticker_thread
    if not _ticker_enabled:
        return
    if _ticker_thread is not None and _ticker_thread.is_alive():
        return
    _ticker_stop = threading.Event()

    def loop() -> None:
        stop = _ticker_stop
        while stop is not None and not stop.wait(1.0):
            poll_waiting()

    _ticker_thread = threading.Thread(  # health: allow HX012 -- stdlib-only pre-swap updater
        target=loop, name="hermes-update-waiting", daemon=True)
    _ticker_thread.start()


def _stop_ticker() -> None:
    global _ticker_stop, _ticker_thread
    if _ticker_stop is not None:
        _ticker_stop.set()
    _ticker_stop = None
    _ticker_thread = None
