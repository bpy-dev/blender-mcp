# Private per-agent Tailcat sessions (opt-in runtime transport)

The default image entrypoint remains offline MCP stdio. Explicitly replace its
command with `--transport tailcat` to enable this transport. Tailcat **v0.7.0** is
baked at build time, separately SHA-256-verified for Linux AMD64 and ARM64 by
`tailcat_install.py`; startup never installs packages. No Tailscale account is
needed. The existing exact buildbpy wheel lock and MCP tools are unchanged.

## Bootstrap and trust boundary

There are two independent identities. Generate a fresh client key on the
originating agent's machine for each session; its private key never goes in a
prompt, log, image, Pod, provider environment, or uploaded evidence. The runtime
receives only that client's public key, in a mandatory single-key allowlist.

**Chosen minimal bootstrap:** the originating agent also generates a separate,
fresh server key/PSK with a fixed DERP region. It provisions **only this server
identity** through the authenticated provider launch environment. The agent
already holds its capability address, so no address is printed in provider logs,
no unauthenticated callback is needed, and no provider exec API is required.
This is deliberately *provisioned server identity*, not a claim that the server
generated a secret unknown to the agent. Server-generation would require an
additional authenticated return channel; this slice does not invent one.

Trust the originating host, provider HTTPS/control plane, and container-host
administrators. Environment values can be available to authorized provider
operators, Pod API readers and host admins. Prefer the provider's secret-reference
mechanism when already available; ordinary authenticated launch env is **not** a
secret vault and must not be described as one. Runpod documents
[`{{ RUNPOD_SECRET_name }}`](https://docs.runpod.io/pods/templates/secrets)
in [Pod environment variables](https://docs.runpod.io/pods/templates/environment-variables).
No secret-management API scope was assumed or widened here. Review this bootstrap
trust boundary before any paid launch; if provider-managed encrypted secrets are
mandatory, arranging one is an additional pre-provisioning gate, not permission
to copy the client key to the Pod or log the address.

Readiness is a successful authenticated MCP `initialize`, not a log line or a
provider 'running' state. Server public identity and PSK come from the trusted
local generator; both are bound to the authorized launch. The capability address
contains the PSK and remains private even with the client allowlist.

## Local preparation and launch

Run the originating-agent helper with a verified native Tailcat v0.7.0 executable:

```sh
python3 docker/runtime/tailcat_prepare.py --directory "$PRIVATE_SESSION_DIR" \
  --tailcat /absolute/path/to/tailcat --ttl 900 --region 302
```

Use a **new**, private directory outside the checkout/evidence tree. It is created
0700, and generated files are 0600. The helper uses isolated HOME/XDG directories.
It creates `client/tailcat/keys/client-default.private.json`, the independent
server key, private `server.address`, `provider-env.json`, and `docker.env`.
The last two contain the public client identity, server key and absolute expiry;
they do **not** contain the client private key. Never `cat` these files into agent
context or attach them as artifacts. Delete the whole private directory after
teardown. Do not commit a private session directory.

For a local disposable image test:

```sh
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --env-file "$PRIVATE_SESSION_DIR/docker.env" \
  --mount type=bind,src="$EMPTY_OUTPUT_DIR",dst=/workspace \
  "$REVIEWED_IMAGE_DIGEST" --transport tailcat
```

The output directory must be writable by UID/GID 10001. Add `--gpus all` only
when required; never mount Docker sockets, host credentials or unrelated files.
MCP artifacts default private (umask 077); explicitly transfer intended outputs
through MCP or choose appropriate artifact permissions. Do not relax key modes.

A local client process must use HOME and XDG_CONFIG_HOME set to the private
`client` subdirectory, and pass the privately read `server.address` and port `80`
to the baked-version Tailcat CLI. Its stdio is unchanged MCP JSON-RPC. Launch it
programmatically without echoing argv, and discard/capture stderr privately:
Tailcat client diagnostics can include capabilities too. `tailcat_smoke.py`
contains an executable example which never sends client key files to the server.

At a provider, read `provider-env.json` in local code and construct an authenticated
launch payload without printing it. Select the published **new image digest** and
explicit command `--transport tailcat`. A generic create/read/delete Pod API is
sufficient for this approach; provider exec permission stays unchanged. Do not
persist full create/read responses or `docker inspect` container config in public
evidence, because they may contain env secrets. Provider startup overrides and
nonroot workspace ownership still require live provider validation.

## Fail-closed lifetime and capabilities

- `BPY_TAILCAT_ALLOW`: exactly one nonzero lowercase `nodekey:` public identity.
  Empty, malformed, wildcard, `none`, and multi-client values fail before launch.
- `BPY_TAILCAT_SERVER_KEY`: a fresh v0.7.0 server-key JSON with nonzero private
  key, server public key and PSK, and a fixed positive DERP region. No defaults or
  saved identities are inherited. `TAILCAT_ADDR_FILE` is never inherited.
- `BPY_TAILCAT_EXPIRES_AT`: absolute UTC Unix seconds, in the future and no more
  than one hour away. Expired/restarted sessions cannot reset the deadline.
  Cold startup consumes the budget. A monotonic timer enforces admitted duration.
- The listener serves only a fixed MCP stdio command as UID 10001. No shell,
  exit node, SSH server, forwarded host ports, or client-chosen executable.
- All upstream stdout/stderr are suppressed except an exact fixed
  `bpy: authenticated MCP session started` event. This is an audit event, not
  readiness. No capability-address regex sanitization is relied upon.
- Each connection starts a fresh MCP process. Reconnect with the same identity
  before expiry is supported; this is not in-memory-session resumption.
  Disconnect sends EOF; an in-flight render may persist until completion or TTL.
- At TTL or TERM/INT, terminate then kill the listener process group and reap
  adopted normal descendants. This bounds ordinary MCP/bpy children; authorized
  arbitrary Python can deliberately escape a process group. **Authentication and
  process cleanup are not a code/container sandbox.** MCP can execute Python,
  read/write permitted files and use the network. Host administrators can read
  container secrets; there is no 'only the agent can access anything' claim.

## Verification and delivery gates

```sh
python3 -m unittest discover -s tests -p 'test_runtime_*.py' -v
python3 docker/runtime/tailcat_smoke.py --image bpy-dev-runtime:tailcat-candidate \
  --output /absolute/disposable/evidence --gpu
```

The host smoke needs Pillow and Docker. It copies the image's baked Tailcat binary
to a private host directory; no startup download or client identity in the server.
It tests wrong identity with the correct address and zero MCP dispatch events,
allowed initialize/tools/inspect/CPU image, reconnect, active MCP/bpy TTL cleanup,
post-expiry rejection and malformed-startup rejection. Optional native GPU mode
adds the historical CUDA-only image tool plus a second MCP render with explicit
post-render CUDA-only telemetry, both 512x384/12 samples. Portable tests separately
prove process-group reaping outside Docker so namespace teardown cannot hide it.

Both native architecture CI jobs retain the offline stdio CPU smoke and add this
networked transport gate before trusted publication of the tested archives.
Existing anonymous manifests/layer downloads and immutable child-digest pull
verification remain required. Public relay availability is a real gate dependency;
these tests do not certify forced relay, cross-host NAT, or provider GPU execution.
No paid Pods are provisioned by these scripts.

After independent review, protected delivery, exact-main dual-native gates and
public digest verification, a separate bounded two-Pod experiment may record
create/pull/initialize/first-render timings. Hold the new digest constant across
those launches; it differs from the old stdio-only image. Call a repeat a cache
hit only with provider pull-log evidence, not just faster startup. Retain failed
attempts, cumulative costs, a teardown watchdog and exact owned-resource deletion
readback. Never widen denied provider exec scope or modify unrelated resources.
