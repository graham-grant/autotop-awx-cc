#!/usr/bin/env python3
"""Stands in for the `ssh` binary when collecting from Windows: emulates
Windows OpenSSH running `powershell -NoProfile -NonInteractive
-EncodedCommand <base64>`. It asks for the password (FAKE_WIN_PASSWORD,
default "secret"), decodes the script it was given, and answers with the
output a fixture recorded for that script (FAKE_WIN_FIXTURE: a facts file
whose "cli" is {"powershell:<name>.ps1": json}). The script is identified
by comparing its text with scripts/drivers/powershell/, so a script that
isn't one of those gets an error.

Every remote command is appended to $FAKE_SSH_LOG as a JSON line with the
decoded script, so tests can check what the "server" received.
FAKE_WIN_MODE=noise prints PowerShell progress noise around the output."""
import base64
import hashlib
import json
import os
import sys
import tty
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts" / "drivers" / "powershell"


def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def password():
    buf = ""
    while True:
        c = sys.stdin.read(1)
        if c in ("\r", "\n", ""):
            out("\r\n")
            return buf
        buf += c


argv = sys.argv[1:]
user = argv[argv.index("-l") + 1] if "-l" in argv else "?"
remote = argv[argv.index("powershell"):] if "powershell" in argv else []
tty.setraw(sys.stdin.fileno())
out(f"{user}@server's password: ")
if password() != os.environ.get("FAKE_WIN_PASSWORD", "secret"):
    out(f"{user}@server: Permission denied (password,keyboard-interactive).\r\n")
    sys.exit(255)

if remote[:4] != ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand"] \
        or len(remote) != 5:
    out("'unexpected' is not recognized as an internal or external command\r\n")
    sys.exit(1)
script = base64.b64decode(remote[4]).decode("utf-16-le")
if os.environ.get("FAKE_SSH_LOG"):
    with open(os.environ["FAKE_SSH_LOG"], "a") as f:
        f.write(json.dumps({"remote": remote[:4], "script": script,
                            "sha256": hashlib.sha256(script.encode()).hexdigest()}) + "\n")
name = next((p.name for p in sorted(SCRIPTS.glob("*.ps1"))
             if p.read_text(encoding="utf-8") == script), None)
cli = json.load(open(os.environ["FAKE_WIN_FIXTURE"]))["cli"]
if name is None or f"powershell:{name}" not in cli:
    out("Get-Something : The term is not recognized\r\n")
    sys.exit(1)
if os.environ.get("FAKE_WIN_MODE") == "noise":
    out("#< CLIXML\r\n<Objs Version=\"1.1.0.1\"><Obj S=\"progress\"/></Objs>\r\n")
out("AUTOTOP-BEGIN\r\n" + cli[f"powershell:{name}"] + "\r\nAUTOTOP-END\r\n")
sys.exit(0)
