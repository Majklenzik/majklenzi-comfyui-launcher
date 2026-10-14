# syntax=docker/dockerfile:1
# ComfyUI + Launcher (port 3000) + ComfyUI (8188) + JupyterLab (8888) for RunPod.
FROM python:3.12-slim-bookworm

# Build arguments. cu128 wheels carry their own CUDA runtime, so no CUDA base image is needed.
# Pods need an NVIDIA driver that supports CUDA 12.8 or newer.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
ARG COMFYUI_REPO=https://github.com/Comfy-Org/ComfyUI.git
ARG COMFYUI_REF=v0.40.0

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates ffmpeg build-essential libgl1 libglib2.0-0 \
      openssh-server tini nano procps \
 && rm -rf /var/lib/apt/lists/* \
 # every pod must get its own SSH host keys (start.sh runs ssh-keygen -A), not the ones baked into the image
 && rm -f /etc/ssh/ssh_host_* \
 && sed -i 's@session\s*required\s*pam_loginuid.so@session optional pam_loginuid.so@' /etc/pam.d/sshd

# 1) PyTorch (CUDA build)
RUN pip install torch torchvision torchaudio --index-url ${TORCH_INDEX}

# 2) ComfyUI itself. It lives in /opt and is copied to the volume on first boot (see start.sh).
RUN git clone --depth 1 --branch ${COMFYUI_REF} ${COMFYUI_REPO} /opt/ComfyUI \
 && pip install -r /opt/ComfyUI/requirements.txt \
 && if [ -f /opt/ComfyUI/manager_requirements.txt ]; then pip install -r /opt/ComfyUI/manager_requirements.txt; fi

# 3) JupyterLab and the launcher
RUN pip install jupyterlab
COPY requirements-launcher.txt /opt/launcher/requirements-launcher.txt
RUN pip install -r /opt/launcher/requirements-launcher.txt
COPY launcher /opt/launcher/launcher
COPY start.sh /start.sh
RUN sed -i 's/\r$//' /start.sh && chmod +x /start.sh      # survives a Windows checkout with CRLF line endings

# From here on, packages that users install at runtime (custom node requirements) go to the volume,
# so they survive a pod restart together with the custom_nodes folder. Set after the build steps on purpose.
ENV WORKSPACE=/workspace \
    PYTHONUSERBASE=/workspace/.pylibs \
    PIP_USER=1

WORKDIR /workspace
EXPOSE 3000 8188 8888 22

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/start.sh"]
