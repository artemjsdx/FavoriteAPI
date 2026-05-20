import os
import sys
import subprocess
import shutil

base = "/home/container"

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

sys.path.insert(0, base)
os.chdir(base)

import runpy
runpy.run_path(os.path.join(base, "api.py"), run_name="__main__")
