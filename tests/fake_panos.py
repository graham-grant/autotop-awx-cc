"""A fake PAN-OS XML API (firewall or Panorama) for the tests, on a local
port. It answers type=keygen for admin/secret and serves op responses from
a fixture's "cli" map ({request: response XML}). Every request it receives
is appended to `received` (with the key and password replaced), so tests
can compare what the "device" got with collect's audit.

    with FakePanos(fixture) as api:          # api.port, api.received
        ...
    FakePanos(fixture, serial="0079...")     # Panorama: requests must carry
                                             # target=<serial>
    FakePanos(fixture, config_change=True)   # a new commit job appears after
                                             # the first jobs request

Run as a script to serve a fixture over HTTPS for a manual playbook run:
    python3 fake_panos.py FIXTURE PORT CERT KEY [SERIAL HOSTNAME]
"""
import json
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "LUFRPT1fakeapikey0123456789=="
JOBS = "type=op&cmd=<show><jobs><all></all></jobs></show>"
CONNECTED = "type=op&cmd=<show><devices><connected></connected></devices></show>"
ERROR = ('<response status="error" code="17"><msg><line><![CDATA[ show -> {} is an '
         'unexpected keyword]]></line></msg></response>')


def _ok(inner):
    return f'<response status="success"><result>{inner}</result></response>'


class FakePanos:
    def __init__(self, fixture, serial=None, hostname="pa-fw1", config_change=False,
                 username="admin", password="secret", tls=None):
        self.cli = dict(fixture["cli"])
        self.serial, self.hostname = serial, hostname
        self.config_change = config_change
        self.username, self.password = username, password
        self.tls = tls  # (certfile, keyfile)
        self.received = []
        self._jobs_calls = 0
        self.server = None

    def respond(self, params: dict) -> str:
        kind = params.get("type")
        if kind == "keygen":
            if params.get("user") == self.username and params.get("password") == self.password:
                return _ok(f"<key>{KEY}</key>")
            return ('<response status="error" code="403"><result><msg>Invalid Credential'
                    '</msg></result></response>')
        if params.get("key") != KEY:
            return '<response status="error" code="403"><msg>Invalid key</msg></response>'
        target = params.get("target")
        request = urllib.parse.unquote_plus(urllib.parse.urlencode(
            [(k, v) for k, v in params.items() if k not in ("key", "target")]))
        if self.serial is not None:  # Panorama
            if request == CONNECTED and target is None:
                return _ok(f"<devices><entry name='{self.serial}'><serial>{self.serial}</serial>"
                           f"<hostname>{self.hostname}</hostname><connected>yes</connected>"
                           "</entry></devices>")
            if target != self.serial:
                return '<response status="error"><msg>No such device</msg></response>'
        if request == JOBS and self.config_change:
            self._jobs_calls += 1
            if self._jobs_calls > 1:
                return self.cli[JOBS].replace(
                    "<job>", "<job><tenq>2026/10/26 01:00:05</tenq><id>99</id>"
                    "<type>Commit</type><status>FIN</status><result>OK</result></job><job>", 1)
        if request in self.cli:
            return self.cli[request]
        return ERROR.format(request)

    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                params = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
                logged = {k: ("****" if k in ("key", "password") else v)
                          for k, v in params.items()}
                fake.received.append({"path": self.path, "query": self.path.partition("?")[2],
                                      **logged})
                reply = fake.respond(params).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", getattr(self, "_port", 0)), Handler)
        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*self.tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    fixture_path, port, cert, key = sys.argv[1:5]
    fake = FakePanos(json.load(open(fixture_path)), serial=(sys.argv[5:6] or [None])[0],
                     hostname=(sys.argv[6:7] or ["pa-fw1"])[0], tls=(cert, key))
    fake._port = int(port)
    with fake:
        print(f"fake PAN-OS API on https://127.0.0.1:{fake.port}/api/", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
