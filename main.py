import os
import sys
import subprocess
import shutil
import urllib.request
import stat

base = "/home/container"

# --- 1. Restore freeapi/ from GitHub if missing ---
if not os.path.isdir(os.path.join(base, "freeapi")):
    print("[setup] freeapi/ missing - restoring from GitHub...", flush=True)
    subprocess.run(
        ["git", "clone", "--depth=1", "https://github.com/artemjsdx/FavoriteAPI.git", "/tmp/_fapi"],
        check=True
    )
    shutil.copytree("/tmp/_fapi/freeapi", os.path.join(base, "freeapi"))
    if not os.path.isdir(os.path.join(base, "static")) and os.path.isdir("/tmp/_fapi/static"):
        shutil.copytree("/tmp/_fapi/static", os.path.join(base, "static"))
    print("[setup] freeapi/ restored!", flush=True)

# --- 2. Install cloudflared if missing ---
cf_bin = os.path.join(base, ".local", "bin", "cloudflared")
if not os.path.isfile(cf_bin):
    os.makedirs(os.path.dirname(cf_bin), exist_ok=True)
    print("[setup] Downloading cloudflared...", flush=True)
    url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    urllib.request.urlretrieve(url, cf_bin)
    os.chmod(cf_bin, os.stat(cf_bin).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("[setup] cloudflared installed!", flush=True)

# Add .local/bin to PATH so cloudflared is found
os.environ["PATH"] = os.path.join(base, ".local", "bin") + ":" + os.environ.get("PATH", "")

sys.path.insert(0, base)
os.chdir(base)

import runpy
runpy.run_path(os.path.join(base, "api.py"), run_name="__main__")
