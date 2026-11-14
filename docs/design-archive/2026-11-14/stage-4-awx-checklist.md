# Stage 4 checklist: the topology diagram, published to Bitbucket

Created 2026-11-14 · Status: ready to run · Part of the [production roadmap](production-roadmap.md)

## Overview

This produces the first topology diagram from AWX and commits it to a repository in the internal Bitbucket. It starts with the dummy devices, so the whole job, including the commit to Bitbucket, is proved before the diagram job contacts a real device. It uses `playbooks/gather_topology.yml` from this repository, unchanged.

Collection is exactly stage 1's: the same playbook (`collect_facts.yml`, imported), the same commands, the same safeguards. Nothing in this checklist changes a device's configuration. The job writes to one place only, the Bitbucket repository you give it.

Steps marked **(your environment)** need something only you have: AWX, Bitbucket, credentials, the inventory or the devices.

## A. Set up (once)

1. **(your environment)** **Bitbucket repository.** Create a repository for the diagrams, or choose one, and make sure its branch has at least one commit (creating it with a README is enough). The job commits to `main` unless you set `bitbucket_branch`. Note its HTTPS clone URL, such as `https://<bitbucket host>/scm/<project>/<repository>.git`.
2. **(your environment)** **Account and token.** Choose the account the job commits as, ideally a service account, with write access to that repository only. Create an HTTP access token for it with repository write permission. The job needs the account's user name and the token.
3. **(your environment)** **AWX credential type.** In AWX, **Administration → Credential Types → Add**, named for example `Bitbucket`:

   Input configuration:

   ```yaml
   fields:
     - {id: username, type: string, label: Username}
     - {id: token, type: string, label: Token, secret: true}
   required: [username, token]
   ```

   Injector configuration:

   ```yaml
   env:
     BITBUCKET_USERNAME: "{{ username }}"
     BITBUCKET_TOKEN: "{{ token }}"
   ```

   Then create a credential of that type with the user name and token from step 2.
4. **(your environment)** **Code.** Bring the project in AWX up to `main` after this change is merged (section G lists the files, if you copy them across by hand), and sync it.
5. **(your environment)** **git in the execution environment.** The job needs the `git` command. If you run `preflight.yml`, its versions list shows `git version ...` and `C extension .../_hanan.abi3.so`. Otherwise, if git is missing, the job itself fails at "Fail if the repository couldn't be cloned" with `No such file or directory: b'git'`, before any device is contacted.
6. **(your environment)** **Network path and certificate.** The execution environment must reach Bitbucket over HTTPS. If Bitbucket's certificate is signed by an internal CA the execution environment doesn't trust, the clone fails with a certificate error: put the CA certificate in a file the job can read and set `bitbucket_ca_file` to its path. Don't turn verification off.

## B. Dummy run: no real device (gate H1)

7. **(your environment)** **Job template.** Create a job template for `playbooks/gather_topology.yml` with the dummy inventory (the one sourced from `dummy/inventory`), the Network credential used for the stage 1 dummy run, and the Bitbucket credential. In **Variables**:

   ```yaml
   bitbucket_repo_url: https://<bitbucket host>/scm/<project>/<repository>.git
   bitbucket_path: topology-dummy
   ```

   The URL must not contain a user name or password; the job refuses one that does.
8. **(your environment)** **Run it.** The job should succeed, and its log should show, in order:
   - "Clone the publish repository": ok;
   - the stage 1 output: the plan, four hosts collected, `audit: 4 host(s), 56 line(s) sent in total, 0 failed`, `CHECK PASS 4 file(s)`;
   - under "Show the build output": `Drew 4 nodes and 2 links`, four `feature ...: ok` lines, and three `Wrote ...` lines;
   - `Published to <url>, branch main: A topology-dummy/template.vsdx, A topology-dummy/topology.drawio, A topology-dummy/topology.vsdx`.
9. **(your environment)** **Look in Bitbucket.** The repository has one new commit, `Topology from AWX job <job id>`, with those three files. Download `topology-dummy/topology.vsdx` and open it in Visio: four devices in a row (hq, rtr1, sw1, sw2), two links, and a VLANs layer.
10. **(your environment)** **Run it again.** With nothing changed, the log says `Nothing changed since the last publish, so there is nothing to commit.` and Bitbucket has no new commit.

## C. Development switches (gate H2)

11. **(your environment)** **Job template.** Copy the template, change its inventory to the development switches' inventory and its Network credential to the switches' collection account, and set:

    ```yaml
    bitbucket_repo_url: https://<bitbucket host>/scm/<project>/<repository>.git
    bitbucket_path: topology-dev
    ```

    Use a different folder from the dummy run: each inventory has its own devices and its own layout.
12. **(your environment)** **Run it.** Collection is the stage 1 job you have already run, so the same 14 lines reach each switch. Then `Drew 2 nodes and N links` and `Published to ...`. If the two switches are cabled to each other and run CDP or LLDP, there is a link between them with the interface names at each end; if they aren't, the diagram shows two nodes and no link, which is correct.
13. **(your environment)** **Check the diagram against the switches.** Open `topology-dev/topology.vsdx`. Compare each link's interfaces with `show cdp neighbors` on one switch, and the green or red interface labels with the ports' real state.

If a switch fails collection, the job says `Not published: no facts file from this run for <host>` and fails. That is deliberate: a diagram missing a device would otherwise replace the full one. Fix the host and run again, or set `bitbucket_publish_partial: true` to publish what there is.

## D. Arrange the layout (gate H3)

14. **(your environment)** Download `topology-dev/template.vsdx` from Bitbucket and open it in Visio. It has one shape per device and nothing else. Drag the shapes where you want them, save, and commit the file back to the same path in Bitbucket (upload it in the web interface, or `git push`).
15. **(your environment)** Run the job again. The log has no `placed in a staging grid` note, the commit changes `topology.vsdx` and `topology.drawio`, and the diagram has the devices where you put them.

Keep the template to device shapes only: any other shape with text, such as a title, stops the build. A device removed from the inventory, or renamed, must have its shape deleted from the template too.

## E. What to send back

- the `Drew` line and the `feature` lines;
- the `Published`, `Nothing changed` or `Not published` line;
- whether `topology.vsdx` opens in Visio and matches the cabling;
- the text of any failed task. A failed git task prints git's own error, which never includes the token.

## F. If it fails

| Message | Cause |
| --- | --- |
| `No repository credential in the job environment` | The Bitbucket credential isn't attached to the job template, or its type's injector doesn't set `BITBUCKET_USERNAME` and `BITBUCKET_TOKEN` |
| `Couldn't clone ...: Authentication failed` | Wrong user name or token, or the token lacks access to the repository |
| `Couldn't clone ...: Remote branch main not found` | The repository is empty or its branch has another name: add a first commit, or set `bitbucket_branch` |
| `Couldn't clone ...: SSL certificate problem` | Bitbucket's CA isn't trusted in the execution environment: set `bitbucket_ca_file` |
| `Couldn't clone ...: Could not resolve host` or a timeout | No network path from the execution environment to Bitbucket |
| `bitbucket_repo_url has a password or token in it` | Remove the credential from the URL |
| `Not published: no facts file from this run for ...` | Those inventory devices weren't collected: a failed host, a platform outside `collect_platforms`, or a job limit |
| `shape text(s) don't match any host` | The template has a shape for a device that isn't in the inventory, or a shape with other text: delete it from `template.vsdx` |
| `The push to ... was refused` | The token can read but not write, a branch permission blocks the account, or someone pushed during the job (run it again) |

## G. Files this stage uses

If the project in AWX is a copy of selected files, rather than a sync of the whole repository, stage 4 needs these 47 files. All of them are in this list, including the ones stage 1 already uses, because several of those changed.

From the repository root, this copies them into one folder, `export/`, with `/` written as `+` and `.txt` added. The one binary file, the prebuilt C extension, is written as base64 text:

```bash
mkdir -p export && for f in dummy/bin/ssh dummy/inventory playbooks/collect_facts.yml playbooks/gather_topology.yml playbooks/group_vars/all.yml playbooks/group_vars/network_devices.yml scripts/audit.py scripts/build_ext.py scripts/check_facts.py scripts/collect.py scripts/commands.py scripts/config.py scripts/csrc/_hanan.c scripts/diagram.py scripts/drawio.py scripts/drivers/__init__.py scripts/drivers/common.py scripts/drivers/ios.py scripts/drivers/panos.py scripts/drivers/windows.py scripts/facts.py scripts/features/__init__.py scripts/features/bundling.py scripts/features/context.py scripts/features/ipsec.py scripts/features/porttags.py scripts/features/roles.py scripts/features/vlans.py scripts/features/zones.py scripts/git_askpass.sh scripts/icon_library.py scripts/inventory.py scripts/links.py scripts/main.py scripts/miniyaml.py scripts/pathfinder.py scripts/positions.py scripts/readonly.py scripts/renderer.py scripts/sshcli.py scripts/vsdx.py scripts/xmlapi.py tests/fixtures/facts/hq.json tests/fixtures/l2/rtr1.json tests/fixtures/l2/sw1.json tests/fixtures/l2/sw2.json; do cp "$f" "export/${f//\//+}.txt"; done; base64 scripts/_hanan.abi3.so > export/scripts+_hanan.abi3.so.b64.txt; ls export | wc -l
```

In the destination's project root, with the `.txt` files copied into it, this puts them back:

```bash
for f in *.txt; do p="${f%.txt}"; p="${p//+//}"; mkdir -p "$(dirname "$p")"; case "$p" in *.b64) base64 -d "$f" > "${p%.b64}";; *) cp "$f" "$p";; esac; done; chmod +x dummy/bin/ssh scripts/git_askpass.sh
```

The device icons in `icons/` are needed only when an inventory device sets `icon`. `playbooks/preflight.yml` also needs the whole `tests/` folder.
