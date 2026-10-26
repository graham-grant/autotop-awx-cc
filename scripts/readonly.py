"""
Read-only command policy: the guarantee that collection never changes a
device's configuration.

Every command the collector sends to a device goes through check(), at the
lowest layer (sshcli.CliSession.run), so nothing can reach a device unless
the platform's policy allows it. Three rules, all of which must pass:

  1. allowlist  - the command matches one of the platform's allowed shapes
                  (on IOS: "show ..." with only display filters after a
                  pipe, plus a few session-only "terminal" settings);
  2. denylist   - its first word is not, or abbreviates, a verb that can
                  change state (configure, write, copy, reload, clear, ...).
                  This catches a mistake in an allowlist entry;
  3. characters - no control characters, newlines or other characters that
                  could smuggle a second command onto the line.

A platform without a policy gets nothing: collection refuses to connect.
Adding a platform means adding its policy here, and the command-inventory
test (tests/test_readonly.py) checks every command any driver or feature
can emit against it.

    policy = readonly.policy_for("ios")
    policy.check("show version")            # returns the command
    policy.check("configure terminal")      # raises CommandRefused
"""
from __future__ import annotations

import re


class ReadOnlyViolation(Exception):
    """Something that could change a device was about to happen, and was
    stopped. Never caught and ignored: it aborts collection for the host."""


class CommandRefused(ReadOnlyViolation):
    pass


class UnexpectedPrompt(ReadOnlyViolation):
    """The device asked for a confirmation, which a read-only command never
    does. The prompt is cancelled with Ctrl-C, never answered."""


class ConfigChanged(ReadOnlyViolation):
    """The device's configuration-change marker moved while we were logged
    in."""


# Verbs that change state on some platform. A command whose first word is
# one of these, or an abbreviation of one (IOS accepts "conf t", "wr"), is
# refused even if an allowlist entry would let it through.
DENY_VERBS = (
    # Cisco IOS / IOS-XE / NX-OS
    "configure", "write", "copy", "reload", "delete", "erase", "format",
    "clear", "debug", "undebug", "squeeze", "rename", "mkdir", "rmdir",
    "install", "upgrade", "license", "crypto", "no", "default", "archive",
    "event", "tclsh", "test", "send", "boot", "verify", "request",
    "monitor", "start-shell", "guestshell", "app-hosting", "ping",
    "traceroute", "telnet", "ssh", "connect", "release", "renew",
    # PAN-OS
    "set", "commit", "load", "save", "revert", "edit", "move", "rename",
    # Windows / PowerShell (verb half of Verb-Noun)
    "set-", "new-", "remove-", "add-", "enable-", "disable-", "restart-",
    "stop-", "start-", "invoke-", "install-", "uninstall-", "update-",
    "register-", "unregister-", "rename-", "clear-", "reset-", "suspend-",
    "resume-", "move-", "copy-", "out-file",
)

# Display filters IOS allows after "|". Anything else after a pipe
# ("redirect", "tee", "append") writes a file to the device, so it is refused.
IOS_PIPE_FILTERS = ("include", "exclude", "begin", "section", "count")

_IOS_SHOW = re.compile(r"show( [A-Za-z0-9_./:-]+)+")
_IOS_FILTER = re.compile(r" (%s) [^|?\t]+" % "|".join(IOS_PIPE_FILTERS))
_IOS_SESSION = (
    re.compile(r"terminal length 0"),
    re.compile(r"terminal width (0|[1-9][0-9]{0,2})"),
    re.compile(r"terminal datadump"),     # AT-8000S style switches
)
# Control characters, DEL, and on IOS "?" (help: it leaves the typed line in
# the buffer, so the next command would be appended to it) and tab
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f?]")


class Policy:
    def __init__(self, name: str, allow, deny_verbs=DENY_VERBS, bad_chars=_BAD_CHARS):
        self.name = name
        self._allow = allow          # callable: command -> bool
        self._deny = tuple(deny_verbs)
        self._bad = bad_chars

    def check(self, command: str) -> str:
        """Return the command unchanged if it is safe to send, otherwise
        raise CommandRefused saying which rule it broke."""
        if not isinstance(command, str) or not command.strip():
            raise CommandRefused(f"{self.name}: empty command refused")
        if self._bad.search(command):
            raise CommandRefused(f"{self.name}: {command!r} refused: contains a "
                                 "control character, newline or '?'")
        if command != command.strip() or "  " in command:
            raise CommandRefused(f"{self.name}: {command!r} refused: stray whitespace")
        verb = command.split()[0].lower()
        for denied in self._deny:
            if denied.endswith("-") or denied == "out-file":
                hit = verb.startswith(denied)
            else:
                hit = verb == denied or (len(verb) >= 2 and denied.startswith(verb))
            if hit:
                raise CommandRefused(f"{self.name}: {command!r} refused: "
                                     f"'{verb}' can change the device ({denied})")
        if not self._allow(command):
            raise CommandRefused(f"{self.name}: {command!r} refused: not in the "
                                 "read-only allowlist for this platform")
        return command

    def allows(self, command: str) -> bool:
        try:
            self.check(command)
            return True
        except CommandRefused:
            return False


def _ios_allow(command: str) -> bool:
    if any(p.fullmatch(command) for p in _IOS_SESSION):
        return True
    show, *filters = command.split(" |")
    if not _IOS_SHOW.fullmatch(show):
        return False
    return all(_IOS_FILTER.fullmatch(f) for f in filters)


_POLICIES = {
    "ios": Policy("ios", _ios_allow),
}


def policy_for(platform: str) -> Policy:
    """The read-only policy for a netparse platform key ("ios"). Raises
    CommandRefused if the platform has none, so an unknown platform is
    never contacted."""
    policy = _POLICIES.get((platform or "").lower())
    if policy is None:
        raise CommandRefused(f"no read-only command policy for platform {platform!r}: "
                             "refusing to send it anything. Add one to scripts/readonly.py")
    return policy


def platforms() -> list[str]:
    return sorted(_POLICIES)
