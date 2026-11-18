# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.10.11@sha256:3472e43b4e738cf911c99d41bb34331280efad54c73b1def654a6227bb59b2b4 AS uv
FROM ubuntu:24.04@sha256:73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8 AS runtime
# Ubuntu 24.04 matches the wheel's glibc 2.39 floor. No CUDA toolkit:
# bpy ships Cycles kernels; --gpus all supplies the host driver at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libx11-6 libxxf86vm1 libxfixes3 libxi6 libxrender1 \
    libgl1 libegl1 libsm6 libice6 libxkbcommon0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy UV_NO_PROGRESS=1
RUN uv python install 3.13.11 && uv venv --python 3.13.11 /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY docker/runtime/requirements.lock /opt/runtime/requirements.lock
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --require-hashes --only-binary=:all: \
    -r /opt/runtime/requirements.lock
# Heavy wheel layer is independent of MCP source and user scene files.
ARG TARGETARCH
COPY docker/runtime/release.py docker/runtime/release.lock.json /opt/runtime/
RUN --mount=type=cache,target=/root/.cache/uv \
    python /opt/runtime/release.py --lock /opt/runtime/release.lock.json \
    --download "$TARGETARCH" --directory /opt/wheels \
    && uv pip install --python /opt/venv/bin/python --no-deps /opt/wheels/*.whl \
    && rm -rf /opt/wheels \
    && python -c "import bpy,json; assert bpy.app.version_string.split()[0] == json.load(open('/opt/runtime/release.lock.json'))['version']; print(bpy.app.version_string)"
COPY mcp /opt/source/mcp
RUN uv pip install --python /opt/venv/bin/python --no-deps --no-build-isolation /opt/source/mcp \
    && uv pip check --python /opt/venv/bin/python \
    && rm -rf /opt/source /usr/local/bin/uv
COPY docker/runtime/entrypoint.py docker/runtime/tailcat_session.py /opt/runtime/
COPY docker/runtime/tailcat_install.py /opt/runtime/
RUN python /opt/runtime/tailcat_install.py "$TARGETARCH"
COPY LICENSE NOTICE.md /opt/runtime/
RUN useradd --create-home --uid 10001 runtime && mkdir /workspace && chown runtime:runtime /workspace
ENV BLENDER_MCP_CLI_BACKEND=bpy BLENDER_MCP_BPY_PYTHON=/opt/venv/bin/python \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/opt/venv/bin/python", "/opt/runtime/entrypoint.py"]
CMD ["--transport", "stdio"]
