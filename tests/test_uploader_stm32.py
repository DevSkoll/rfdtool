"""Tests for the STM32 system-memory bootloader uploader.

These tests never invoke the real ``stm32flash``: every test that exercises
``upload_stm32`` substitutes a ``FakeProc`` via the ``subprocess_factory``
hook. ``FakeProc`` is a minimal stand-in for a :class:`subprocess.Popen` with
just enough surface area (``stdout.readline``, ``poll``, ``wait``,
``terminate``, ``stdout.close``) for the uploader to drive it.
"""
from __future__ import annotations

from typing import Callable

import pytest

from rfd.uploader_stm32 import (
    STM32FlashCancelled,
    STM32FlashError,
    stm32flash_available,
    stm32flash_install_hint,
    upload_stm32,
)


# ---------------------------------------------------------------------------
# Fake subprocess
# ---------------------------------------------------------------------------

class FakeProc:
    """Minimal stand-in for subprocess.Popen for the uploader.

    Constructed with the lines stm32flash would emit on stdout (one per
    element, no trailing newline) and the eventual return code. ``readline``
    returns each line with a "\\n" appended, then "" on EOF; after EOF
    ``poll`` returns the configured rc so the uploader exits its loop.
    """

    def __init__(self, lines: list[str], rc: int = 0) -> None:
        self._lines = list(lines)
        self._idx = 0
        self.returncode: int | None = None
        self._rc = rc
        self.terminate_called = False
        # The uploader reads via ``proc.stdout.readline()``; pointing
        # ``stdout`` back at ``self`` means we serve both roles.
        self.stdout = self

    # ---- stdout-like surface --------------------------------------------
    def readline(self) -> str:
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line + "\n"
        return ""

    def close(self) -> None:  # called from the finally block
        pass

    # ---- Popen-like surface ---------------------------------------------
    def poll(self) -> int | None:
        if self._idx >= len(self._lines):
            return self._rc
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._rc
        return self._rc

    def terminate(self) -> None:
        self.terminate_called = True
        # Once terminated, drain remaining lines so poll() reports done.
        self._idx = len(self._lines)


def _make_factory(proc: FakeProc, captured: list[list[str]] | None = None
                  ) -> Callable[[list[str]], FakeProc]:
    """Build a subprocess_factory that returns ``proc`` and (optionally) records args."""
    def factory(args: list[str]) -> FakeProc:
        if captured is not None:
            captured.append(list(args))
        return proc
    return factory


def _write_bin(tmp_path, size: int = 1024, name: str = "fw.bin") -> str:
    """Create a fixed-size binary fixture and return its path as a string."""
    p = tmp_path / name
    p.write_bytes(b"\x00" * size)
    return str(p)


# ---------------------------------------------------------------------------
# Trivial helpers
# ---------------------------------------------------------------------------

def test_install_hint_mentions_apt_install():
    hint = stm32flash_install_hint()
    assert isinstance(hint, str)
    assert "apt install stm32flash" in hint


def test_stm32flash_available_false_for_missing_path():
    # An absolute path that definitely doesn't exist - shutil.which honours
    # absolute paths and returns None when the file isn't executable/present.
    assert stm32flash_available("/no/such/binary") is False


# ---------------------------------------------------------------------------
# upload_stm32 happy path
# ---------------------------------------------------------------------------

def test_upload_stm32_happy_path(tmp_path):
    bin_path = _write_bin(tmp_path, size=1024)
    proc = FakeProc(
        [
            "Wrote address 0x08000000 (10%)",
            "Wrote address 0x08000400 (50%)",
            "Wrote and verified address 0x080003ff (100.00%) Done.",
        ],
        rc=0,
    )

    progress_calls: list[tuple[int, int]] = []
    log_lines: list[str] = []

    upload_stm32(
        "/dev/ttyMOCK",
        bin_path,
        progress=lambda done, total: progress_calls.append((done, total)),
        log=log_lines.append,
        subprocess_factory=_make_factory(proc),
    )

    # 10% of 1024 == 102 (int truncation), 50% == 512, 100% == 1024.
    assert progress_calls == [(102, 1024), (512, 1024), (1024, 1024)]
    assert len(log_lines) == 3
    assert "10%" in log_lines[0]
    assert "50%" in log_lines[1]
    assert "100.00%" in log_lines[2]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_upload_stm32_nonzero_exit_raises_with_tail(tmp_path):
    bin_path = _write_bin(tmp_path, size=1024)
    proc = FakeProc(
        [
            "Wrote address 0x08000000 (10%)",
            "Failed to read ACK",
            "Failed to send CMD frame",
        ],
        rc=1,
    )
    with pytest.raises(STM32FlashError) as exc_info:
        upload_stm32(
            "/dev/ttyMOCK",
            bin_path,
            subprocess_factory=_make_factory(proc),
        )
    msg = str(exc_info.value)
    # Tail should include the failure lines stm32flash printed before exit.
    assert "Failed to read ACK" in msg
    assert "Failed to send CMD frame" in msg
    # And the non-zero exit code is reported.
    assert "1" in msg


def test_upload_stm32_cancel_terminates_proc(tmp_path):
    bin_path = _write_bin(tmp_path, size=1024)
    proc = FakeProc(
        [
            "Wrote address 0x08000000 (10%)",
            "Wrote address 0x08000400 (50%)",
            "Wrote address 0x08000800 (75%)",
            "Wrote and verified address 0x080003ff (100.00%) Done.",
        ],
        rc=0,
    )

    state = {"progress_calls": 0}

    def progress(done: int, total: int) -> None:
        state["progress_calls"] += 1

    def cancel() -> bool:
        # Cancel after the first progress callback has fired.
        return state["progress_calls"] >= 1

    with pytest.raises(STM32FlashCancelled):
        upload_stm32(
            "/dev/ttyMOCK",
            bin_path,
            progress=progress,
            cancel_check=cancel,
            subprocess_factory=_make_factory(proc),
        )

    assert proc.terminate_called is True


def test_upload_stm32_missing_bin_raises(tmp_path):
    missing = str(tmp_path / "does_not_exist.bin")
    # Provide a factory so we don't accidentally hit the missing-binary branch.
    proc = FakeProc([], rc=0)
    with pytest.raises(STM32FlashError) as exc_info:
        upload_stm32(
            "/dev/ttyMOCK",
            missing,
            subprocess_factory=_make_factory(proc),
        )
    assert "firmware file not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Argument construction
# ---------------------------------------------------------------------------

def test_upload_stm32_builds_expected_args(tmp_path):
    bin_path = _write_bin(tmp_path, size=512)
    proc = FakeProc(
        ["Wrote and verified address 0x080001ff (100.00%) Done."],
        rc=0,
    )
    captured: list[list[str]] = []

    upload_stm32(
        "/dev/ttyUSB0",
        bin_path,
        subprocess_factory=_make_factory(proc, captured=captured),
    )

    assert len(captured) == 1
    args = captured[0]

    # Spot-check every flag the spec requires.
    assert args[0] == "stm32flash"
    assert "-b" in args
    assert args[args.index("-b") + 1] == "57600"
    assert "-w" in args
    assert args[args.index("-w") + 1] == bin_path
    assert "-v" in args
    assert "-g" in args
    assert args[args.index("-g") + 1] == "0x08000000"
    # Port is the trailing positional argument.
    assert args[-1] == "/dev/ttyUSB0"


# ---------------------------------------------------------------------------
# Progress monotonicity / completion
# ---------------------------------------------------------------------------

def test_upload_stm32_progress_is_monotonic(tmp_path):
    bin_path = _write_bin(tmp_path, size=1000)
    # The 5% line in the middle must NOT trigger a callback - the bar should
    # never go backwards even if stm32flash restarts a phase.
    proc = FakeProc(
        [
            "Wrote address 0x08000000 (10%)",
            "Verifying address 0x08000000 (5%)",   # backwards: ignored
            "Wrote address 0x08000400 (50%)",
            "Wrote and verified address 0x... (100%) Done.",
        ],
        rc=0,
    )

    progress_calls: list[tuple[int, int]] = []
    upload_stm32(
        "/dev/ttyMOCK",
        bin_path,
        progress=lambda done, total: progress_calls.append((done, total)),
        subprocess_factory=_make_factory(proc),
    )

    # Three calls (10%, 50%, 100%) - the 5% line is skipped.
    assert len(progress_calls) == 3
    pcts = [done for done, _ in progress_calls]
    assert pcts == sorted(pcts)
    assert progress_calls[0] == (100, 1000)
    assert progress_calls[1] == (500, 1000)
    assert progress_calls[2] == (1000, 1000)


def test_upload_stm32_snaps_to_100_on_clean_exit(tmp_path):
    bin_path = _write_bin(tmp_path, size=2048)
    # stm32flash sometimes ends without ever printing 100% (e.g. the very
    # last chunk lands on 99.x%). On a clean rc==0 exit we must still tell
    # the caller the flash is fully done.
    proc = FakeProc(
        [
            "Wrote address 0x08000000 (50%)",
            "Wrote address 0x08000400 (99%)",
            "Done.",
        ],
        rc=0,
    )

    progress_calls: list[tuple[int, int]] = []
    upload_stm32(
        "/dev/ttyMOCK",
        bin_path,
        progress=lambda done, total: progress_calls.append((done, total)),
        subprocess_factory=_make_factory(proc),
    )

    assert progress_calls[-1] == (2048, 2048)
