# Rules for changing this project

These apply to every change, whoever (or whatever) makes it.

## Collection is read-only

Collection must never change a device's configuration. Every command sent to
a device is only for gathering information.

- Every command goes through the platform's policy in `scripts/readonly.py`.
  Never send anything to a device except through `CliSession.run()` (or, for
  later transports, the equivalent guarded call).
- Never weaken a policy, the denylist or the character check to make a
  command pass. If a needed command is refused, find a read-only alternative
  or raise it with the project owner.
- Never answer a confirmation prompt during collection.
- A new platform needs its read-only policy, its config-change marker if it
  has one, and entries in `tests/test_readonly.py`, before it can be
  contacted.
- A new feature's `commands` must pass the policy for each platform it lists.
  `tests/test_readonly.py` checks this.

## Standard library only

Python code imports only the standard library and this project's modules.
No pip packages and no Ansible collections. It must run on Python 3.9 with
no compiler. `scripts/_hanan.abi3.so` is prebuilt and committed. Rebuild it
with `python3 scripts/build_ext.py` whenever `scripts/csrc/_hanan.c` changes.

## Credentials

Credentials come only from environment variables. Never put a password on a
command line, in a file, in a log line or in an exception message, and never
commit one. Mask passwords as `****` in any transcript that is shown.

## Documentation is part of every change

- Update `README.md` in the same change as any behaviour, option, file or
  command it describes. It always describes the current code.
- Design documents go in `docs/design-archive/<YYYY-MM-DD>/`, named for the
  day they were written. Don't rewrite an archived document: put a revision
  in a new dated folder with a "What changed" section, and add it to the
  index in `docs/design-archive/README.md`.

## Tests

Run `python3 -m unittest discover -s tests` before every commit. All tests
must pass, on Python 3.9 as well as newer versions.
