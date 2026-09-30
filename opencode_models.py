from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def find_opencode() -> str | None:
    found = shutil.which("opencode")
    if found:
        return found
    # Apps started from the application menu or systemd do not get the
    # shell's PATH, so also look where the opencode installer puts it.
    fallback = Path.home() / ".opencode" / "bin" / "opencode"
    if fallback.is_file():
        return str(fallback)
    return None


def list_models() -> list[str]:
    """Every model opencode offers, as `provider/model` ids."""
    opencode = find_opencode()
    if opencode is None:
        raise RuntimeError("opencode not found (looked in PATH and ~/.opencode/bin)")

    try:
        result = subprocess.run(
            [opencode, "models"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("opencode models timed out") from None
    except OSError as exc:
        raise RuntimeError(f"Could not run opencode: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(
            f"opencode models failed: {detail[-1] if detail else result.returncode}"
        )

    models = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "/" in line and " " not in line:
            models.append(line)
    return models
