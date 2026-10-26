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

PAN-OS is reached through its XML API rather than a CLI, so its policy
(XmlApiPolicy) checks API request parameters instead; see that class.

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


class XmlApiPolicy:
    """The PAN-OS XML API policy. A request is a query string of the API's
    parameters (the API key and keygen credentials are added by the client
    after the check, and are never part of it):

        type=op&cmd=<show><system><info></info></system></show>
        type=config&action=get&xpath=/config/devices
        type=keygen

      - type=op only with a cmd that is one XML element tree whose root is
        <show>, with no DTD, entities, processing instructions or comments;
      - type=config only with action=get or action=show and an xpath under
        /config;
      - type=keygen alone (the client sends it once, first, to log in);
      - target=<serial> (through Panorama) is the only other parameter, and
        is allowed on op and config requests;
      - any other type (commit, import, export, user-id, report, log, ...)
        or action (set, edit, delete, rename, move, clone, override,
        multi-move, ...) is refused, as is a repeated parameter.
    """

    _SERIAL = re.compile(r"[0-9A-Za-z]{1,32}")
    _BAD = re.compile(r"[\x00-\x1f\x7f]")
    # Markup that isn't plain elements and text: DTDs and entities could
    # expand into something else, and none of it belongs in a show command
    _MARKUP = re.compile(r"<[!?]|&")
    ALLOWED_CONFIG_ACTIONS = ("get", "show")

    def __init__(self, name: str):
        self.name = name

    def _refuse(self, request, why):
        raise CommandRefused(f"{self.name}: {request!r} refused: {why}")

    def check(self, request: str) -> str:
        from urllib.parse import parse_qsl
        import xml.etree.ElementTree as ET

        if not isinstance(request, str) or not request.strip():
            raise CommandRefused(f"{self.name}: empty request refused")
        if self._BAD.search(request) or request != request.strip():
            self._refuse(request, "contains a control character, newline or stray whitespace")
        try:
            pairs = parse_qsl(request, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            self._refuse(request, "not a query string of API parameters")
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            self._refuse(request, "a parameter is repeated")
        params = dict(pairs)
        kind = params.get("type")
        if "target" in params and not self._SERIAL.fullmatch(params["target"]):
            self._refuse(request, "target must be a firewall serial number")
        if kind == "keygen":
            if keys != ["type"]:
                self._refuse(request, "keygen takes no other parameters here")
        elif kind == "op":
            if set(keys) - {"type", "cmd", "target"}:
                self._refuse(request, f"unexpected parameter(s) {sorted(set(keys) - {'type', 'cmd', 'target'})}")
            cmd = params.get("cmd", "")
            if self._MARKUP.search(cmd):
                self._refuse(request, "cmd may only hold plain XML elements and text")
            try:
                root = ET.fromstring(cmd)
            except ET.ParseError:
                self._refuse(request, "cmd is not well-formed XML")
            if root.tag != "show":
                self._refuse(request, f"operational commands must be <show>, not <{root.tag}>")
        elif kind == "config":
            if set(keys) - {"type", "action", "xpath", "target"}:
                self._refuse(request, f"unexpected parameter(s) {sorted(set(keys) - {'type', 'action', 'xpath', 'target'})}")
            if params.get("action") not in self.ALLOWED_CONFIG_ACTIONS:
                self._refuse(request, f"config action {params.get('action')!r} can change "
                                      "the device; only get and show are allowed")
            xpath = params.get("xpath", "")
            if not (xpath == "/config" or xpath.startswith("/config/")):
                self._refuse(request, "xpath must be under /config")
        else:
            self._refuse(request, f"API request type {kind!r} is not read-only")
        return request

    def allows(self, request: str) -> bool:
        try:
            self.check(request)
            return True
        except CommandRefused:
            return False


_POLICIES = {
    "ios": Policy("ios", _ios_allow),
    "panos": XmlApiPolicy("panos"),
}


def policy_for(platform: str) -> Policy:
    """The read-only policy for a driver platform key ("ios"). Raises
    CommandRefused if the platform has none, so an unknown platform is
    never contacted."""
    policy = _POLICIES.get((platform or "").lower())
    if policy is None:
        raise CommandRefused(f"no read-only command policy for platform {platform!r}: "
                             "refusing to send it anything. Add one to scripts/readonly.py")
    return policy


def platforms() -> list[str]:
    return sorted(_POLICIES)
