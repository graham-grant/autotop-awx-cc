"""
PAN-OS XML API sessions, standard library only (urllib, ssl, xml.etree).

The API equivalent of sshcli.CliSession, with the same read-only guarantees
and the same interface, so collect.py treats both alike:

    with ApiSession(host, username, password,
                    policy=readonly.policy_for("panos")) as s:
        xml_text = s.run("type=op&cmd=<show><system><info></info></system></show>")

A request is the query string of its API parameters (see
readonly.XmlApiPolicy). run() sends it only if the session's policy allows
it, and records it in `audit` either way.

Login: one `type=keygen` request, sent first, with the username and
password in the POST body (never in a URL). The API key it returns is kept
in this object only: it goes in the POST body of each later request, never
in a URL, the audit, the log or an error message. Error messages mask the
password and the key as ****.

Through Panorama: give Panorama's address as host and the firewall's serial
number as target. Panorama then proxies each request to that firewall
(`target=<serial>`, which the policy also checks). find_serial() looks the
serial up by hostname among Panorama's connected firewalls.

Certificates are verified by default, against the system's CA store or
ca_file. verify=False is an explicit per-host opt-in for lab firewalls with
self-signed certificates, and logs a warning. Plain http is refused except
to localhost, which the tests' fake API uses.
"""
from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import readonly
from sshcli import AuthError, CliError

KEYGEN = "type=keygen"
CONNECTED_DEVICES = "type=op&cmd=<show><devices><connected></connected></devices></show>"
_LOCAL = ("127.0.0.1", "localhost", "::1")


class ApiSession:
    def __init__(self, host: str, username: str, password: str, port: int = 443,
                 policy: readonly.XmlApiPolicy | None = None, target: str | None = None,
                 verify: bool = True, ca_file: str | None = None, timeout: float = 30.0,
                 command_timeout: float = 120.0, scheme: str = "https", log=print):
        if scheme not in ("https", "http"):
            raise CliError(f"unsupported scheme {scheme!r}")
        if scheme == "http" and host not in _LOCAL:
            raise CliError(f"{host}: refusing plain http to anything but localhost - "
                           "the password would cross the network unencrypted")
        self.host, self.port, self.scheme = host, port, scheme
        self.username, self.password = username, password
        self.policy, self.target = policy, target
        self.timeout, self.command_timeout = timeout, command_timeout
        self.log = log
        self.audit: list[dict] = []
        self._key = None
        self._context = None
        if scheme == "https":
            self._context = ssl.create_default_context(cafile=ca_file)
            if not verify:
                self._context.check_hostname = False
                self._context.verify_mode = ssl.CERT_NONE
                self.log(f"WARNING certificate verification is off for {host} "
                         "(panos_verify_tls: false)")

    # -- plumbing -----------------------------------------------------------

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}/api/"

    def _mask(self, text: str) -> str:
        for secret in (self.password, self._key):
            if secret:
                text = text.replace(secret, "****")
                text = text.replace(urllib.parse.quote_plus(secret), "****")
        return text

    def _record(self, request: str, kind: str, status: str) -> dict:
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "command": request, "kind": kind, "status": status}
        self.audit.append(entry)
        self.log(f"{status:>8}  {request}")
        return entry

    def _post(self, params: list[tuple[str, str]], timeout: float) -> tuple[ET.Element, str]:
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._context) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise AuthError(f"{self.host}: HTTP {e.code} {self._mask(detail)}") from None
            raise CliError(f"{self.host}: HTTP {e.code} {self._mask(detail)}") from None
        except (urllib.error.URLError, OSError) as e:
            raise CliError(f"{self.host}: {self._mask(str(getattr(e, 'reason', e)))}") from None
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise CliError(f"{self.host}: not an XML API response: "
                           f"{self._mask(text[:200])!r}") from None
        return root, text

    @staticmethod
    def _message(root: ET.Element) -> str:
        msgs = [" ".join(e.itertext()).strip() for e in root.iter() if e.tag in ("msg", "line")]
        return "; ".join(m for m in msgs if m) or "no message"

    # -- session ------------------------------------------------------------

    def open(self):
        """Log in: get an API key with type=keygen."""
        if self.policy is None:
            self._record(KEYGEN, "session", "refused")
            raise readonly.CommandRefused(f"{self.host}: no read-only policy for this "
                                          "session, not logging in")
        self.policy.check(KEYGEN)
        entry = self._record(KEYGEN, "session", "sent")
        root, _ = self._post([("type", "keygen"), ("user", self.username),
                           ("password", self.password)], self.timeout)
        key = root.findtext("./result/key")
        if root.get("status") != "success" or not key:
            entry["status"] = "error"
            raise AuthError(f"{self.host}: login refused: {self._mask(self._message(root))}")
        self._key = key
        entry["status"] = "ok"
        return self

    def request_string(self, request: str) -> str:
        """The request as checked, sent and audited: the driver's request
        plus target=<serial> when going through Panorama."""
        return f"{request}&target={self.target}" if self.target else request

    def run(self, request: str, timeout: float | None = None, kind: str = "collect",
            target: bool = True) -> str:
        """Send one read-only request and return the response XML as text.
        Raises readonly.CommandRefused, before anything is sent, if the
        policy doesn't allow it, and CliError if the device answers with an
        error (an unsupported command, say)."""
        full = self.request_string(request) if target else request
        if self._key is None:
            raise CliError("session not open")
        if self.policy is None:
            self._record(full, kind, "refused")
            raise readonly.CommandRefused(f"{self.host}: no read-only policy for this "
                                          f"session, not sending {full!r}")
        try:
            self.policy.check(full)
            if urllib.parse.parse_qs(full).get("type") == ["keygen"]:
                raise readonly.CommandRefused(f"{self.host}: keygen is only sent once, "
                                              "to log in")
        except readonly.CommandRefused:
            self._record(full, kind, "refused")
            raise
        entry = self._record(full, kind, "sent")
        # Exactly the parameters the policy checked, plus the key
        params = urllib.parse.parse_qsl(full, keep_blank_values=True, strict_parsing=True)
        root, text = self._post(params + [("key", self._key)], timeout or self.command_timeout)
        if root.get("status") != "success":
            entry["status"] = "error"
            raise CliError(f"{self.host}: {self._mask(self._message(root))}")
        entry["status"] = "ok"
        return text

    def find_serial(self, hostname: str) -> str:
        """The serial number of the firewall Panorama knows as `hostname`
        (asked of Panorama itself, without a target)."""
        root = ET.fromstring(self.run(CONNECTED_DEVICES, kind="lookup", target=False))
        found = []
        for dev in root.iter("entry"):
            names = {(dev.findtext(t) or "").strip().lower() for t in ("hostname", "devicename")}
            if hostname.lower() in names and dev.findtext("serial"):
                found.append(dev.findtext("serial").strip())
        if len(found) != 1:
            raise CliError(f"{self.host}: Panorama has {len(found) or 'no'} connected "
                           f"firewall(s) named {hostname!r}; set panos_serial for it")
        return found[0]

    def disable_paging(self, commands):
        """Nothing to do: the API doesn't page."""

    def close(self):
        self._key = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
