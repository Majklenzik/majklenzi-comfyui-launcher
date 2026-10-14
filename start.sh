#!/usr/bin/env bash
# Container entry point: prepare the volume, start sshd + JupyterLab, then run the launcher in the
# foreground. The launcher starts and supervises ComfyUI.
set -eo pipefail

export WORKSPACE="${WORKSPACE:-/workspace}"
mkdir -p "$WORKSPACE" "$WORKSPACE/.launcher"
cd "$WORKSPACE"

# 1) ComfyUI is copied to the volume once, so models, nodes and workflows persist.
if [ ! -f "$WORKSPACE/ComfyUI/main.py" ]; then
  echo "[start] first boot: copying ComfyUI to $WORKSPACE/ComfyUI"
  # Copy to a temporary name and rename at the end, so a pod stopped mid-copy never leaves a half ComfyUI.
  rm -rf "$WORKSPACE/.ComfyUI.tmp"
  cp -a /opt/ComfyUI "$WORKSPACE/.ComfyUI.tmp"
  if [ -e "$WORKSPACE/ComfyUI" ]; then
    if [ -d "$WORKSPACE/ComfyUI" ] && [ -z "$(ls -A "$WORKSPACE/ComfyUI")" ]; then
      rmdir "$WORKSPACE/ComfyUI"
    else
      aside="$WORKSPACE/ComfyUI.old-$(date +%s)"
      echo "[start] $WORKSPACE/ComfyUI has no main.py. Keeping it as $aside"
      mv "$WORKSPACE/ComfyUI" "$aside"
    fi
  fi
  mv "$WORKSPACE/.ComfyUI.tmp" "$WORKSPACE/ComfyUI"
fi

# 2) Python packages installed at runtime go to the volume (PYTHONUSERBASE). The folder must exist
#    before Python starts or it is not added to sys.path.
mkdir -p "$(python -c 'import site; print(site.getusersitepackages())')"

# 3) SSH (RunPod sets PUBLIC_KEY when an SSH key is saved in the account settings).
if [ -n "${PUBLIC_KEY:-}" ]; then
  mkdir -p /root/.ssh /run/sshd
  chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys
  grep -qxF "$PUBLIC_KEY" /root/.ssh/authorized_keys || echo "$PUBLIC_KEY" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  ssh-keygen -A >/dev/null 2>&1
  /usr/sbin/sshd
  echo "[start] sshd is running"
fi

# 4) JupyterLab on 8888. The proxy URL is public, so a token is always required.
if [ "${ENABLE_JUPYTER:-1}" = "1" ]; then
  if [ -z "${JUPYTER_PASSWORD:-}" ]; then
    JUPYTER_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(18))')"
    echo "[start] JUPYTER_PASSWORD is not set. Generated a token for this run: $JUPYTER_PASSWORD"
  fi
  # -s: JupyterLab ignores packages that custom nodes installed on the volume (they could shadow its libraries)
  nohup python -s -m jupyterlab --allow-root --no-browser --ip=0.0.0.0 --port="${JUPYTER_PORT:-8888}" \
      --ServerApp.root_dir="$WORKSPACE" \
      --ServerApp.allow_origin='*' --ServerApp.allow_remote_access=True \
      --ServerApp.terminado_settings='{"shell_command":["/bin/bash"]}' \
      --IdentityProvider.token="$JUPYTER_PASSWORD" \
      > "$WORKSPACE/.launcher/jupyter.log" 2>&1 &
  echo "[start] JupyterLab starting on port ${JUPYTER_PORT:-8888}"
fi

# 5) Launcher in the foreground (PID 1 is tini, so signals and zombies are handled).
cd /opt/launcher
echo "[start] launcher on port ${LAUNCHER_PORT:-3000}"
exec python -s -m launcher       # -s: node requirements on the volume can never shadow the launcher's libraries
