FROM node:22-bookworm

# System deps. node:22-bookworm (non-slim) already carries the build toolchain
# native modules like bcrypt need; we just add git/jq/curl. No `gh` — issue text
# is injected from the host, so the container needs no GitHub token.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Align the image's `agent` user with the host UID/GID so files written into the
# bind-mounted worktree are host-owned, not root. `sandcastle docker build-image`
# defaults these build-args to the host user's UID/GID.
ARG AGENT_UID=1000
ARG AGENT_GID=1000
RUN groupmod -o -g "$AGENT_GID" node \
 && usermod  -o -u "$AGENT_UID" -g "$AGENT_GID" -d /home/agent -m -l agent node
USER ${AGENT_UID}:${AGENT_GID}

# Claude Code CLI (authenticated at runtime via CLAUDE_CODE_OAUTH_TOKEN).
# Pinned version — bump deliberately after testing, don't float to latest.
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- 2.1.205
ENV PATH="/home/agent/.local/bin:${PATH}"

# uv for the Python toolchain (winnow is a Python/FastAPI project; uv manages
# the interpreter and venv per worktree, so no system Python is needed).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /home/agent

# Sandcastle bind-mounts the git worktree at /home/agent/workspace and overrides
# the working directory there at container start. It keeps the container alive
# and execs the agent into it.
ENTRYPOINT ["sleep", "infinity"]
