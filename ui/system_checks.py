"""Startup environment checks for serial-port access on Linux.

Pure data layer — no Qt.  Each ``check_*`` function returns a
:class:`SystemIssue` if it finds a problem the user should know about, or
``None`` if everything looks fine.  ``run_all_checks()`` runs every check
and returns the aggregate list; the UI layer decides how (or whether) to
surface the results.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemIssue:
    severity: str           # "info" | "warn" | "error"
    title: str
    detail: str             # multi-line, may include shell snippets


_MODEMMANAGER_DETAIL = """\
ModemManager is running and will probe newly-attached USB serial adapters,
which can disrupt or corrupt the first few seconds of an RFD900 connection.

Recommended fix — remove ModemManager if you don't need it:

    sudo apt remove modemmanager

If you need to keep ModemManager (e.g. for a cellular modem), tell it to
ignore FTDI and Silicon Labs adapters by adding a udev rule:

    # /etc/udev/rules.d/99-rfdtool.rules
    ATTRS{idVendor}=="0403", ENV{ID_MM_DEVICE_IGNORE}="1"
    ATTRS{idVendor}=="10c4", ENV{ID_MM_DEVICE_IGNORE}="1"

Then reload udev and replug the adapter:

    sudo udevadm control --reload-rules
    sudo udevadm trigger
"""


_DIALOUT_DETAIL = """\
Your user account is not a member of the 'dialout' group, so opening
/dev/ttyUSB* devices will fail with "Permission denied".

Fix:

    sudo usermod -aG dialout $USER
    # then log out and back in for the new group to take effect
"""


def check_modemmanager_running() -> SystemIssue | None:
    """Detect a running ModemManager service.

    Uses ``systemctl is-active`` so the check is harmless on systems without
    ModemManager installed (it will simply report "inactive" or fail), and on
    systems without systemd at all (the call raises and we return None).
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "ModemManager"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    except Exception:
        return None

    if result.returncode == 0 and result.stdout.strip() == "active":
        return SystemIssue(
            severity="warn",
            title="ModemManager is running",
            detail=_MODEMMANAGER_DETAIL,
        )
    return None


def check_dialout_membership() -> SystemIssue | None:
    """Verify the current user is in the 'dialout' group.

    Returns ``None`` on platforms without a dialout group (Windows/macOS) or
    if the lookup fails for any reason — better to stay silent than to nag.
    """
    try:
        import grp

        dialout_gid = grp.getgrnam("dialout").gr_gid
    except (KeyError, ImportError, OSError):
        return None
    except Exception:
        return None

    try:
        groups = os.getgroups()
    except (AttributeError, OSError):
        return None

    if dialout_gid in groups:
        return None

    return SystemIssue(
        severity="warn",
        title="User not in 'dialout' group",
        detail=_DIALOUT_DETAIL,
    )


def run_all_checks() -> list[SystemIssue]:
    """Run every check, return non-None results in declaration order."""
    checks = (
        check_modemmanager_running,
        check_dialout_membership,
    )
    issues: list[SystemIssue] = []
    for fn in checks:
        try:
            issue = fn()
        except Exception:
            # A buggy check should never break startup.
            continue
        if issue is not None:
            issues.append(issue)
    return issues
