"""python -m launcher"""
import uvicorn

from . import config, settings
from .app import create_app


def main() -> None:
    config.ensure_dirs()
    generated = settings.ensure_password()
    if generated:
        bar = "=" * 70
        print(f"{bar}\nLauncher password (user name: anything): {generated}\n"
              f"Also saved in {config.DATA_DIR / settings.PASSWORD_FILE_NAME}\n"
              f"Set LAUNCHER_PASSWORD in the pod environment to choose your own.\n{bar}", flush=True)
    elif config.PASSWORD_SOURCE == "none":
        print("WARNING: LAUNCHER_ALLOW_NO_AUTH=1, the launcher is open to anyone who has the link.", flush=True)
    uvicorn.run(create_app(), host=config.LAUNCHER_HOST, port=config.LAUNCHER_PORT, log_level="info",
                access_log=False, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
