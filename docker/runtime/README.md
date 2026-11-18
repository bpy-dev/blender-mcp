# Prebaked bpy.dev MCP runtime (review candidate)

This belongs to `bpy-dev/blender-mcp`, not Comfy3D or a generic scene service.
It runs MCP stdio with the standalone bpy backend. It does not expose a public
HTTP service or install anything at startup. Run untrusted scene code only with
explicit, non-sensitive mounts; MCP code execution is not a security sandbox.

## Build and run

```sh
python3 docker/runtime/release.py --tag v5.3.0 --lock docker/runtime/release.lock.json
python3 -m unittest discover -s tests -p 'test_runtime_*.py' -v
docker build -f docker/runtime/Dockerfile -t bpy-dev-runtime:local .
# MCP clients must keep stdin open; scene paths inside tools use /workspace.
docker run --rm -i --network none --cap-drop ALL --security-opt no-new-privileges \
  --mount type=bind,src="$PWD/scenes",dst=/workspace bpy-dev-runtime:local
```

Add `--gpus all` on a compatible NVIDIA host for CUDA. Default UID/GID is
10001; grant only the intended scene/output directory access. Never mount the
Docker socket, host credentials, or an entire home directory. No credentials are
required in the build context, build arguments, or runtime image.

## Base and cache boundaries

The Ubuntu 24.04 multiarch index and uv image are pinned by registry digest.
The Linux wheels require glibc 2.39; Debian Bookworm is too old. CPython is pinned
to 3.13.11 using the pinned uv distribution/checksum catalog. Python dependencies
are version/hash locked (MCP versions constrained by the existing `mcp/uv.lock`).
Refresh explicitly, review the lock diff, then rerun native gates:

```sh
uv export --project mcp --frozen --no-dev --no-emit-project -o /outside/worktree/mcp-deps.txt
uv pip compile mcp/pyproject.toml docker/runtime/requirements.in \
  --constraint /outside/worktree/mcp-deps.txt --python-version 3.13 --universal \
  --generate-hashes -o docker/runtime/requirements.lock
```

Ubuntu apt packages currently use the normal signed archive, **not an immutable
snapshot**. The recipe is input-pinned for Python/wheels/base, not a claim of
bit-for-bit reproducibility of cold rebuilds. Preserve `dpkg-query -W` alongside
image receipts; snapshot/pin OS packages before requiring byte-identical builds.

Layer order: OS libraries → Python → locked dependencies → verified bpy wheel →
MCP code → tiny entrypoint. User scenes are mounted, never baked. MCP source-only
changes do not invalidate the heavy wheel layer. BuildKit cache is an optimization,
not a trust root: downloads must still match the release lock when executed.

Native Spark testing demonstrated that plain Ubuntu plus the wheel's bundled
libraries/Cycles kernels renders on GB10 with host NVIDIA driver 580.178.04.
`ldd` reported no missing linked libraries. Thus this image does **not** include a
CUDA toolkit, cuDNN, PyTorch, ComfyUI, models, or compilers. Driver compatibility
on other GPUs still requires testing; do not infer it from the Spark result.

## Release-triggered rebuilds, without cross-repository credentials

`runtime-image.yml` polls the public `michaelgold/buildbpy` releases hourly and
also supports exact manual tags and main-source changes. It enumerates *all*
complete releases in the explicitly qualified **5.3.x / cp313** series. New
minor/major Blender releases fail closed until their Python ABI/platform matrix
is reviewed. This is deliberate, not a `latest`/PyPI fallback.

For each tag admission requires a published, non-prerelease GitHub release and
one uploaded, nonempty SHA256-bearing wheel for Linux ARM64, Linux x86_64,
macOS ARM64, and Windows x86_64. Names and download repository are fixed by code.
The lock binds Linux wheel names, byte sizes and SHA256. Before downloading the
builder re-reads release metadata, refuses changes, streams to a partial file,
validates size/hash, and only then permits installation. GitHub is the provenance
trust root; this is not an independently signed upstream attestation.

Polling handles publication sequencing: upstream creates a release before all
platform uploads finish, and uploads made using `GITHUB_TOKEN` do not reliably
start another workflow via ordinary release events. An incomplete release is
retried next hour. A changed Linux asset digest creates a different cache key.
Unchanged trusted-main runs skip only after freshly verifying the anonymous GHCR
index, both child manifests/configs, their content hashes, native architectures,
and source/release labels. There is no success-receipt cache whose lifetime can
outlast the seven-day image artifacts. Missing/private/damaged images do not skip.
Pull requests always build and test both native architectures, with read-only
permissions and no registry login. Require the stable `Runtime native CPU gates`
check in branch protection; it fails if discovery or either native build fails.

## Public GHCR candidates (CPU qualified, not GPU certified)

After both native GitHub-hosted builds and offline MCP entrypoint CPU renders pass,
a separate trusted-main-only job loads those exact tested archives and publishes
`ghcr.io/bpy-dev/bpy-runtime`. Only that job has `packages: write`; login uses CI's
built-in `GITHUB_TOKEN`. No PAT, copied DockerHub token, new key, self-hosted runner,
or RunPod expenditure is needed. Fork and same-repository PRs cannot publish.

Candidate tags are `sha-<full MCP source SHA>-<full release-lock SHA256>` with
`-amd64`/`-arm64` child tags. The lock hash binds both Linux wheel hashes. Existing
child tags must match the tested local image config digest; an existing index must
match the two exact child manifest digests. Tags are input-bound but registry
administrators can mutate tags: consumers must pin the resulting **index digest**,
not a floating tag. No `latest` or production/GPU-certified tag is published.
Source-only changes retain the heavy-wheel BuildKit cache boundary.

Both child and index writes first perform an **authenticated** registry read with
manifest/index Accept types. Only HTTP 404 with exactly one `MANIFEST_UNKNOWN`
error authorizes creation; authentication failures, timeouts, 5xx, other 404 codes
and malformed replies abort without writing. Anonymous discovery errors may still
cause a rebuild, but never authorize publication. Existing matching tags are
reused and mismatched tags are rejected. Workflow concurrency serializes this
workflow's main-branch writers; registry tag pushes have no compare-and-swap.
This is not race-free against an external writer changing the tag between the
read and push. Restrict other package writers and consume immutable digests.

A newly created GHCR package normally starts **private**. Its package owner may
need to set visibility to public in GitHub package settings after the first push.
This workflow does not invent a visibility API or change account credentials. It
fails the publication gate unless an unauthenticated registry client can read the
index and both children, stream and hash-check every compressed layer from GHCR
for both architectures without consulting Docker's cached content, and then pull
both architectures by their immutable child digests bound to the verified index,
with an empty Docker credential directory. Distinct child references avoid the
classic Docker store's same-index-reference cross-platform collision. The layer
download receipts bind architecture, digest and byte size;
the Docker pulls alone do not establish cold downloads because daemon layers may
already be cached. The first visibility failure is
not successful public publication. After making the package public, rerun the
failed publish job while the same run's tested archives still exist. Do not rerun
all jobs merely to recover visibility: rebuilt OS layers can differ, and existing
input-bound tags deliberately reject different bytes. Retain publication JSON and
index/child metadata from the successful run (30-day artifacts).

The native local ARM64/GB10 evidence predates this publication workflow and remains
separate. GitHub-hosted CPU tests do **not** certify recurring CUDA compatibility.
No Comfy3D runner is borrowed; future AMD64 RunPod GPU/cold-pull/cache tests need
separate authorization and the cumulative budget. Public publication is a
prerequisite, not permission to provision. Cache retention/node reuse is not
promised by this image or by a successful local warm start.

## Acceptance

`docker/runtime/smoke.py` runs a real MCP stdio session against the existing tiny
`benchmarks/token_efficiency/fixtures/render_source.blend`, requests a bounded
64×48, two-sample render through `execute_blender_code_for_cli`, checks the exact
bpy version and Python ABI, and retains PNG/device/timing receipts. `--gpu`
requires CUDA devices and disables CPU devices; it never silently falls back.
Use the image's actual entrypoint as the server command:

```sh
mkdir -p evidence && chmod 777 evidence
docker run --rm --network none --gpus all --cap-drop ALL --security-opt no-new-privileges \
  --mount type=bind,src="$PWD",dst=/test,readonly \
  --mount type=bind,src="$PWD/evidence",dst=/evidence \
  --entrypoint /opt/venv/bin/python bpy-dev-runtime:local \
  /test/docker/runtime/smoke.py --gpu \
  --fixture /test/benchmarks/token_efficiency/fixtures/render_source.blend \
  --output /evidence /opt/venv/bin/python /opt/runtime/entrypoint.py
```

The writable evidence directory above is disposable; use narrower ownership in
real deployments. Image startup is network-free; downloads and installs occur
only during baking. Local readiness timings are not cloud cold-pull timings.
