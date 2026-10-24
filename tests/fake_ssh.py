#!/usr/bin/env python3
"""Stands in for the `ssh` binary in test_sshcli: emulates a switch that
asks for the SSH password, then logs in again in-band ("User Name:" /
"Password:"), pages output with an AT-8000S style pager, and knows
"terminal datadump". FAKE_SSH_MODE=wrongpass rejects the in-band login;
FAKE_SSH_MODE=plain skips the in-band login (IOS-like)."""
import os
import sys
import termios
import tty

MODE = os.environ.get("FAKE_SSH_MODE", "inband")
PAGER = "More: <space>,  Quit: q or CTRL+Z, One line: <return> "


def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def line(echo=True):
    buf = ""
    while True:
        c = sys.stdin.read(1)
        if c in ("\r", "\n", ""):
            out("\r\n")
            return buf
        if echo:
            out(c)
        buf += c


tty.setraw(sys.stdin.fileno())
out("admin@sw's password: ")
if line(echo=False) != "secret":
    out("Permission denied, please try again.\r\n")
    sys.exit(255)
if MODE != "plain":
    out("\r\n\r\nUser Name:")
    user = line()
    out("Password:")
    pw = line(echo=False)
    if user != "admin" or pw != "secret" or MODE == "wrongpass":
        out("Login invalid\r\n\r\nUser Name:")
        line()
        sys.exit(1)
paging = True
while True:
    out("AT-8K-1# ")
    cmd = line()
    if cmd == "terminal datadump":
        paging = False
    elif cmd == "show interfaces status":
        for i in range(1, 13):
            out(f"g{i}  1G-Copper  Full  1000  Up\r\n")
            if paging and i % 5 == 0:
                out(PAGER)
                sys.stdin.read(1)
                out("\r" + " " * len(PAGER) + "\r")
    elif cmd == "show version":
        out("SW version    3.0.0.44\r\n")
    elif cmd in ("exit", "logout"):
        sys.exit(0)
    elif cmd:
        out("% Unrecognized command\r\n")
