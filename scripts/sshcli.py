"""
Interactive CLI sessions over SSH, standard library only.

Why this exists: network devices don't behave like servers when you SSH
in. Running each command as a separate SSH exec request (what
ansible.builtin.raw does, via sshpass) depends on Ansible's connection
variables resolving correctly and on the device accepting the password the
way sshpass expects - when either goes wrong, all you get back is
"Invalid/incorrect password". Some devices also ask for "User Name:" /
"Password:" again inside the session, page output with "--More--", or need
"enable". NAPALM coped via netmiko/paramiko, which are third-party: one
interactive session per device, typing like a person would.

This drives the system `ssh` client (present on every AWX execution
environment) through a pseudo-terminal from the `pty` module, and answers
prompts the way a person at a terminal would:

    with CliSession(host, username, password,
                    policy=readonly.policy_for("ios")) as s:
        s.disable_paging(["terminal length 0", "terminal datadump"])
        text = s.run("show version")

Credentials are written to the pty, never put on a command line.

Read-only by construction: run() sends a command only if the session's
readonly.Policy allows it (no policy, nothing is sent), never answers a
confirmation prompt (it cancels it with Ctrl-C and raises), and records
every line it sends to the device in `audit`. The only other things ever
typed are the login answers, "enable" and "exit".
"""
from __future__ import annotations

import errno
import os
import pty
import re
import select
import shutil
import signal
import time
from datetime import datetime, timezone

import readonly

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>]")
# Paging prompts, removed from captured output after we answer them
_MORE = re.compile(rb"(?:--\s*More\s*--|<--- More --->|"
                   rb"More: <space>,\s*Quit: q[^,]*,\s*One line: <return>\s?)"
                   rb"(?:\x08+\s*\x08+|\r\s+\r)?", re.IGNORECASE)
# A pager prompt still waiting for a key (nothing printed after it yet)
_MORE_WAITING = re.compile(rb"(?:--\s*More\s*--|<--- More --->|One line: <return>)\s*$",
                           re.IGNORECASE)
_USER_PROMPT = re.compile(rb"(?:user\s*name|username|login)\s*:\s*$", re.IGNORECASE)
_PASS_PROMPT = re.compile(rb"password\s*:\s*$", re.IGNORECASE)
_HOSTKEY_PROMPT = re.compile(rb"continue connecting.*\?\s*$", re.IGNORECASE)
# A CLI prompt at the end of the buffer: "AT-8K-1#", "core1>", "sw(config)#"
_CLI_PROMPT = re.compile(rb"(?:^|[\r\n])([^\r\n]{1,80}?[>#])\s*$")
# A device asking us to confirm something. A read-only command never does
# this, so it is cancelled, never answered: "[confirm]", "[yes/no]",
# "(y/n)", "Destination filename [startup-config]?"
_CONFIRM_PROMPT = re.compile(rb"(?:\[confirm\]|\[yes/no\]|\(yes/no\)\??|\[y/n\]|\(y/n\)\??|"
                             rb"\]\?)\s*:?\s*$", re.IGNORECASE)
_CTRL_C = b"\x03"
_AUTH_FAILED = re.compile(rb"(permission denied|authentication failed|access denied|"
                          rb"login invalid|bad (user ?name|password)|% ?login invalid)",
                          re.IGNORECASE)


class CliError(RuntimeError):
    pass


class AuthError(CliError):
    pass


class CliSession:
    def __init__(self, host: str, username: str, password: str, port: int = 22,
                 enable_password: str | None = None, ssh_args: list[str] | None = None,
                 timeout: float = 30.0, command_timeout: float = 120.0,
                 ssh_binary: str = "ssh", policy: readonly.Policy | None = None,
                 log=None):
        self.host, self.port = host, int(port)
        self.username, self.password = username, password
        self.enable_password = enable_password
        self.ssh_args = list(ssh_args or [])
        self.timeout = timeout
        self.command_timeout = command_timeout
        self.ssh_binary = ssh_binary
        self.pid = None
        self.fd = None
        self.prompt: bytes | None = None
        self.transcript = bytearray()  # everything received, for error messages
        self.policy = policy
        self.log = log or (lambda line: None)
        # Every line typed at the device after login, credentials excepted:
        # [{"time", "command", "kind": "session"|"collect", "status"}]
        self.audit: list[dict] = []
        self._abandoned = None  # reason, once the session can't be trusted

    # -- process / io ----------------------------------------------------

    def _spawn(self):
        if not shutil.which(self.ssh_binary):
            raise CliError(f"'{self.ssh_binary}' not found - install the OpenSSH client")
        argv = [
            self.ssh_binary, "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=keyboard-interactive,password",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", f"ConnectTimeout={int(self.timeout)}",
            "-o", "ServerAliveInterval=15",
            "-p", str(self.port),
            *self.ssh_args,
            f"{self.username}@{self.host}",
        ]
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd

    def _send(self, text: str):
        os.write(self.fd, text.encode() + b"\r")

    def _read_some(self, deadline: float) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b""
        ready, _, _ = select.select([self.fd], [], [], min(remaining, 1.0))
        if not ready:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except OSError as e:
            if e.errno == errno.EIO:  # child exited
                raise CliError(f"{self.host}: connection closed\n{self._tail()}")
            raise
        if not data:
            raise CliError(f"{self.host}: connection closed\n{self._tail()}")
        self.transcript += data
        return data

    def _record(self, command: str, kind: str, status: str) -> dict:
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "command": command, "kind": kind, "status": status}
        self.audit.append(entry)
        self.log(f"{status:>8}  {command}")
        return entry

    def _tail(self, n=600) -> str:
        text = _ANSI.sub(b"", bytes(self.transcript[-n:])).decode("utf-8", "replace")
        for secret in (self.password, self.enable_password):
            if secret:
                text = text.replace(secret, "****")
        return text

    # -- login -----------------------------------------------------------

    def open(self):
        self._spawn()
        buf = b""
        deadline = time.monotonic() + self.timeout
        password_prompts = user_prompts = 0
        while True:
            if time.monotonic() > deadline:
                raise CliError(f"{self.host}: timed out waiting to log in\n{self._tail()}")
            data = self._read_some(deadline)
            if not data:
                continue
            buf += data
            clean = _ANSI.sub(b"", buf).rstrip(b" ")
            if _AUTH_FAILED.search(clean):
                raise AuthError(f"{self.host}: login rejected\n{self._tail()}")
            if _HOSTKEY_PROMPT.search(clean):
                self._send("yes")
            elif _PASS_PROMPT.search(clean):
                # 1st: the SSH-level prompt; 2nd: an in-band login after a
                # "User Name:" prompt. More than that means it was refused.
                password_prompts += 1
                if password_prompts > 2 + user_prompts:
                    raise AuthError(f"{self.host}: password rejected\n{self._tail()}")
                self._send(self.password)
            elif _USER_PROMPT.search(clean):
                user_prompts += 1
                if user_prompts > 2:
                    raise AuthError(f"{self.host}: username rejected\n{self._tail()}")
                self._send(self.username)
            elif _MORE_WAITING.search(clean):
                os.write(self.fd, b" ")  # paged login banner
            else:
                m = _CLI_PROMPT.search(clean)
                if not m:
                    continue
                self.prompt = m.group(1).strip()
                break
            buf = b""
            deadline = time.monotonic() + self.timeout

        if self.prompt.endswith(b">") and self.enable_password:
            self._enable()
        return self

    def _enable(self):
        self._record("enable", "session", "sent")
        self._send("enable")
        deadline = time.monotonic() + self.timeout
        buf = b""
        while time.monotonic() < deadline:
            buf += self._read_some(deadline)
            clean = _ANSI.sub(b"", buf).rstrip(b" ")
            if _PASS_PROMPT.search(clean):
                self._send(self.enable_password)
                buf = b""
            elif (m := _CLI_PROMPT.search(clean)) and m.group(1).strip().endswith(b"#"):
                self.prompt = m.group(1).strip()
                return
        raise CliError(f"{self.host}: 'enable' failed\n{self._tail()}")

    # -- commands --------------------------------------------------------

    def _prompt_re(self) -> re.Pattern:
        # Hostname part of the prompt, any mode suffix: "sw#", "sw(config)#", "sw>"
        base = re.escape(re.sub(rb"[>#]$", b"", self.prompt).split(b"(")[0])
        return re.compile(rb"(?:^|[\r\n])" + base + rb"(?:\([^)]*\))?[>#]\s*$")

    def run(self, command: str, timeout: float | None = None, kind: str = "collect") -> str:
        """Run one command and return its output, without the echoed
        command or the trailing prompt. Raises readonly.CommandRefused,
        before anything is sent, if the session's policy doesn't allow it."""
        if self.prompt is None:
            raise CliError("session not open")
        if self._abandoned:
            raise CliError(f"{self.host}: session abandoned ({self._abandoned}), "
                           f"not sending {command!r}")
        if self.policy is None:
            self._record(command, kind, "refused")
            raise readonly.CommandRefused(f"{self.host}: no read-only policy for this "
                                          f"session, not sending {command!r}")
        try:
            self.policy.check(command)
        except readonly.CommandRefused:
            self._record(command, kind, "refused")
            raise
        prompt_re = self._prompt_re()
        entry = self._record(command, kind, "sent")
        self._send(command)
        deadline = time.monotonic() + (timeout or self.command_timeout)
        buf = b""
        while True:
            if time.monotonic() > deadline:
                entry["status"] = "timeout"
                self._cancel(prompt_re, f"timed out running {command!r}")
                raise CliError(f"{self.host}: timed out running {command!r}\n{self._tail()}")
            data = self._read_some(deadline)
            if not data:
                continue
            buf += data
            tail = _ANSI.sub(b"", buf[-200:])
            if _MORE_WAITING.search(tail):
                os.write(self.fd, b" ")
                continue
            if prompt_re.search(tail.rstrip(b" ")):
                break
            if _CONFIRM_PROMPT.search(tail.rstrip(b" ")):
                entry["status"] = "cancelled"
                self._cancel(prompt_re, f"{command!r} asked for confirmation")
                self._abandoned = f"{command!r} asked for confirmation"
                raise readonly.UnexpectedPrompt(
                    f"{self.host}: {command!r} asked for confirmation, which a read-only "
                    f"command never does. Cancelled with Ctrl-C, not answered.\n{self._tail()}")
        entry["status"] = "ok"
        return self._clean(buf, command, prompt_re)

    def _cancel(self, prompt_re: re.Pattern, why: str):
        """Send Ctrl-C and wait for the prompt to come back, so nothing we
        send next can be taken as the answer to whatever the device was
        waiting for. If it doesn't come back, stop using the session."""
        self.log(f"{'ctrl-c':>8}  ({why})")
        try:
            os.write(self.fd, _CTRL_C)
        except OSError:
            self._abandoned = why
            return
        deadline = time.monotonic() + min(self.timeout, 10.0)
        buf = b""
        while time.monotonic() < deadline:
            try:
                buf += self._read_some(deadline)
            except CliError:
                break
            if prompt_re.search(_ANSI.sub(b"", buf[-200:]).rstrip(b" ")):
                return
        self._abandoned = why

    @staticmethod
    def _clean(buf: bytes, command: str, prompt_re: re.Pattern) -> str:
        text = _ANSI.sub(b"", buf)
        text = _MORE.sub(b"", text)
        # Apply backspaces some devices use to erase the pager prompt
        prev = None
        while prev != text:
            prev, text = text, re.sub(rb"[^\x08\n]\x08", b"", text)
        text = text.replace(b"\x08", b"")
        # Terminal semantics: a bare \r returns to the start of the line, so
        # keep only what follows the last one (pagers erase their prompt so)
        text = re.sub(rb"\r+\n", b"\n", text)
        lines = [l.rsplit("\r", 1)[-1] for l in text.decode("utf-8", "replace").split("\n")]
        # Drop the echoed command (first line containing it) and the prompt
        if lines and command in lines[0]:
            lines = lines[1:]
        while lines and (not lines[-1].strip() or prompt_re.search(lines[-1].encode())):
            lines.pop()
        return "\n".join(lines) + "\n"

    def disable_paging(self, commands: list[str]):
        """Try each paging-off command; unknown ones just error harmlessly.
        Paging prompts are answered regardless, so this is an optimisation.
        A command the policy refuses still raises."""
        for cmd in commands:
            try:
                self.run(cmd, timeout=self.timeout, kind="session")
            except CliError:
                pass

    def close(self):
        if self.fd is None:
            return
        try:
            if self.prompt is not None:
                self._record("exit", "session", "sent")
            self._send("exit")
            time.sleep(0.2)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        self.fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
