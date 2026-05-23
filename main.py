import os
import sys
import subprocess
import shutil
import secrets

REPO_URL = "https://github.com/artemjsdx/FavoriteAPI.git"
TMP_DIR = "/tmp/_fav_app"
BASE_DIR = "/home/container"

DEFAULT_ENV = {
    "PORT": "8080",
    "DATABASE_PATH": "database.db",
    "TG_NOTIFY_TOKEN": "8225688485:AAFkZuaEZKmBFIGI69x0osW7tHmEMQ1Qak8",
    "TG_NOTIFY_CHATS": "3771442024",
}


def _ensure_env():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        return
    secret = secrets.token_hex(32)
    parts = ["SESSION_SECRET=" + secret]
    for k, v in DEFAULT_ENV.items():
        parts.append(k + "=" + v)
    with open(env_path, "w") as f:
        f.write("\n".join(parts) + "\n")
    print("[boot] .env created with defaults", flush=True)


def _update_repo():
    git_dir = os.path.join(TMP_DIR, ".git")
    if os.path.isdir(git_dir):
        r = subprocess.run(
            ["git", "-C", TMP_DIR, "pull", "--ff-only"],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            msg = r.stdout.strip() or "already up to date"
            print("[boot] GitHub pull OK:", msg, flush=True)
            return
        print("[boot] Pull failed, re-cloning...", flush=True)
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print("[boot] Cloning from GitHub...", flush=True)
    subprocess.run(
        ["git", "clone", "--depth=1", REPO_URL, TMP_DIR],
        check=True
    )
    print("[boot] Clone done!", flush=True)


def _sync_files():
    skip = {".git", "main.py"}
    for name in os.listdir(TMP_DIR):
        if name in skip:
            continue
        src = os.path.join(TMP_DIR, name)
        dst = os.path.join(BASE_DIR, name)
        try:
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except Exception as e:
            print("[boot] Warning: could not copy", name, e, flush=True)
    print("[boot] Files synced to /home/container", flush=True)


_update_repo()
_sync_files()
_ensure_env()

sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import runpy
runpy.run_path(os.path.join(BASE_DIR, "api.py"), run_name="__main__")
