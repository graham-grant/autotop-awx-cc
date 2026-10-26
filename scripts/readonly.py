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
(XmlApiPolicy) checks API request parameters instead, and Windows runs
fixed PowerShell scripts whose every command PowerShellPolicy checks; see
those classes.

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


class PowerShellPolicy:
    """The Windows policy. A command names one of the fixed PowerShell
    scripts in scripts/drivers/powershell/, optionally pinned to its
    SHA-256 (the session records the pinned form in the audit):

        powershell:windows_core.ps1
        powershell:windows_core.ps1#sha256=3f2a...

    The script is read from the repository and checked statement by
    statement before anything connects, and the session sends exactly the
    text that was checked (as -EncodedCommand). A script passes only if:

      - every command it runs is in ALLOWED_COMMANDS: a list of Get-*
        cmdlets that only read, plus the formatting cmdlets;
      - Where-Object and ForEach-Object only take a script block (which is
        checked like the rest; a bare member name would call a method);
      - it assigns only to plain variables, never to an object's property;
      - it has no call operator (&), dot-sourcing, method calls (.Name()),
        static members (::), type literals other than [ordered] and
        [pscustomobject], double-quoted or here-strings (no interpolation),
        subexpressions, redirection (> <), backticks, block comments or
        any character outside the plain PowerShell it needs.
    """

    ALLOWED_COMMANDS = {
        "get-ciminstance", "get-netadapter", "get-netipaddress", "get-netneighbor",
        "get-netlbfoteam", "get-netlbfoteammember", "get-windowsfeature",
        "convertto-json", "select-object", "where-object", "foreach-object", "sort-object",
    }
    KEYWORDS = {"if", "elseif", "else", "foreach", "in"}
    TYPES = {"[ordered]", "[pscustomobject]"}
    _NAME = re.compile(r"powershell:([a-z0-9_-]+\.ps1)(?:#sha256=([0-9a-f]{64}))?")
    _TOKEN = re.compile(r"""
        (?P<nl>\r?\n) | (?P<ws>[ \t]+) | (?P<comment>\#[^\r\n]*) |
        (?P<str>'(?:[^'\r\n]|'')*') |
        (?P<var>\$[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*) |
        (?P<num>\d+) |
        (?P<param>-[A-Za-z]\w*) |
        (?P<type>\[[A-Za-z]+\]) |
        (?P<word>[A-Za-z_]\w*(?:-[A-Za-z_]\w*)?) |
        (?P<punct>@\{|@\(|[{}();|,=.])
    """, re.VERBOSE)
    _COMMAND_START = {None, "nl", ";", "|", "{", "(", "@(", "="}

    def __init__(self, name: str, script_dir):
        self.name = name
        self.script_dir = script_dir

    def _refuse(self, what, why):
        raise CommandRefused(f"{self.name}: {what!r} refused: {why}")

    def script(self, command: str) -> str:
        """The text of the script a command names, after checking it."""
        import hashlib
        from pathlib import Path

        m = self._NAME.fullmatch(command or "")
        if not m:
            self._refuse(command, "not powershell:<script>.ps1 naming a fixed script")
        path = Path(self.script_dir) / m.group(1)
        if not path.is_file():
            self._refuse(command, f"no script {m.group(1)} in {self.script_dir}")
        text = path.read_text(encoding="utf-8")
        if m.group(2) and hashlib.sha256(text.encode("utf-8")).hexdigest() != m.group(2):
            self._refuse(command, "the script has changed since this was recorded")
        self.check_script(text, m.group(1))
        return text

    def check_script(self, text: str, label: str = "script") -> None:
        tokens = []  # (kind, value), whitespace and comments dropped
        pos = 0
        while pos < len(text):
            m = self._TOKEN.match(text, pos)
            if not m:
                self._refuse(label, f"character {text[pos]!r} at offset {pos} is not allowed")
            kind = m.lastgroup
            value = m.group()
            if kind in ("ws", "comment"):
                pass
            elif kind == "nl":
                tokens.append(("nl", "nl"))
            else:
                tokens.append((kind, value))
            pos = m.end()

        stack = []          # open brackets: "hash", "block", "paren"
        prev = None         # previous significant token value (or kind for nl)
        for i, (kind, value) in enumerate(tokens):
            nxt = tokens[i + 1] if i + 1 < len(tokens) else (None, None)
            at_command = prev in self._COMMAND_START
            if kind == "var" and "." in value and nxt[1] == "(":
                self._refuse(label, f"method call {value}(")
            if kind == "var" and "." in value and nxt[1] == "=":
                self._refuse(label, f"assigning to a property ({value}) can change an object")
            if kind == "type" and value.lower() not in self.TYPES:
                self._refuse(label, f"type literal {value}")
            if kind == "punct":
                if value == "." and (at_command or nxt[1] in ("(",) or nxt[0] != "word"):
                    self._refuse(label, "dot-sourcing or a method call")
                if value == "." and i + 2 < len(tokens) and tokens[i + 2][1] == "(":
                    self._refuse(label, f"method call .{nxt[1]}(")
                if value in ("@{",):
                    stack.append("hash")
                elif value == "{":
                    stack.append("block")
                elif value in ("(", "@("):
                    stack.append("paren")
                elif value in ("}", ")"):
                    if not stack:
                        self._refuse(label, f"unbalanced {value}")
                    stack.pop()
            if kind == "word":
                lower = value.lower()
                if prev == ".":
                    pass  # a property name
                elif "-" in lower and lower not in self.ALLOWED_COMMANDS:
                    self._refuse(label, f"{value} is not an allowed command")
                elif at_command:
                    if stack and stack[-1] == "hash" and nxt[1] == "=":
                        pass  # a hashtable key
                    elif lower in self.KEYWORDS:
                        pass
                    elif lower not in self.ALLOWED_COMMANDS:
                        self._refuse(label, f"{value} is not an allowed command")
                    elif lower in ("where-object", "foreach-object") and nxt[1] != "{":
                        self._refuse(label, f"{value} must be given a script block")
            prev = "nl" if kind == "nl" else value
        if stack:
            self._refuse(label, "unbalanced brackets")

    def check(self, command: str) -> str:
        self.script(command)
        return command

    def allows(self, command: str) -> bool:
        try:
            self.check(command)
            return True
        except CommandRefused:
            return False


def _script_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent / "drivers" / "powershell"


_POLICIES = {
    "ios": Policy("ios", _ios_allow),
    "panos": XmlApiPolicy("panos"),
    "windows": PowerShellPolicy("windows", _script_dir()),
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
