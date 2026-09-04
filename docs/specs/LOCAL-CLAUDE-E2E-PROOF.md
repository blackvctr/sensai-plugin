# Local Claude E2E proof

The local production E2E can ask the server operator to verify a hash of the
Telegram reply.  The reply itself never leaves the local runner.

Before that explicit run, create the two files below yourself.  Do not create
them from the plugin, and do not put them in this repository.

`~/.config/sensai/` must be owned by the local user, must not be a symbolic
link, and must not be writable by group or others.  Create it with `0700`.

`~/.config/sensai/local-e2e-proof-ssh.json` must be a regular, user-owned
file with mode `0600` and exactly this shape:

```json
{"schema":"sensai-local-e2e-ssh-v1","host":"proof-host.example"}
```

`~/.config/sensai/local-e2e-proof-known_hosts` must also be a regular,
user-owned `0600` file, not a symbolic link.  Put only the approved server
host key there.

The runner ignores user SSH configuration, global host keys, proxies,
forwarding, and remote commands.  It invokes only the server's fixed proof
program and supplies only a reply SHA-256 on standard input.  A person with
root or Docker-administrator control of the server remains outside this local
client boundary.
