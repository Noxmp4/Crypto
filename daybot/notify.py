"""Notifications système macOS (Centre de notifications) ; sans effet ailleurs."""
from __future__ import annotations

import shutil
import subprocess
import sys

# Le texte passe en argument (argv) et non dans le script AppleScript :
# aucun risque d'injection via un nom de paire ou un message d'erreur.
_SCRIPT = ["-e", "on run argv",
           "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
           "-e", "end run"]


def notify(title: str, message: str, enabled: bool = True) -> bool:
    if not enabled or sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    try:
        subprocess.run(["osascript", *_SCRIPT, title, message],
                       check=False, timeout=5, capture_output=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
