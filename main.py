import os
  import sys
  import subprocess
  import shutil

  base = "/home/container"

  # --- 1. Restore or update freeapi/ from GitHub ---
  _FREEAPI = os.path.join(base, "freeapi")
  _TUNNEL_PY = os.path.join(_FREEAPI, "tunnel.py")

  def _tunnel_is_ok():
      if not os.path.exists(_TUNNEL_PY):
          return False
      txt = open(_TUNNEL_PY, encoding="utf-8").read()
      # bad if uses paramiko OR has escaped quotes (broken patch)
      if "import paramiko" in txt:
          return False
      if '\\"\\"\\"' in txt or '\\"\\"\\"\\"' in txt:
          return False
      # good if uses subprocess.Popen (subprocess version)
      return "subprocess.Popen" in txt

  def _clone_freeapi():
      tmp = "/tmp/_fapi"
      if os.path.exists(tmp):
          shutil.rmtree(tmp)
      print("[setup] Cloning freeapi/ from GitHub...", flush=True)
      subprocess.run(
          ["git", "clone", "--depth=1",
           "https://github.com/artemjsdx/FavoriteAPI.git", tmp],
          check=True
      )
      if os.path.exists(_FREEAPI):
          shutil.rmtree(_FREEAPI)
      shutil.copytree(os.path.join(tmp, "freeapi"), _FREEAPI)
      static_src = os.path.join(tmp, "static")
      static_dst = os.path.join(base, "static")
      if not os.path.isdir(static_dst) and os.path.isdir(static_src):
          shutil.copytree(static_src, static_dst)
      print("[setup] freeapi/ ready!", flush=True)

  if not _tunnel_is_ok():
      reason = "missing" if not os.path.exists(_FREEAPI) else "outdated tunnel.py"
      print(f"[setup] freeapi/ {reason} -- restoring from GitHub...", flush=True)
      _clone_freeapi()
  else:
      print("[setup] freeapi/ OK (subprocess tunnel)", flush=True)

  sys.path.insert(0, base)
  os.chdir(base)

  import runpy
  runpy.run_path(os.path.join(base, "api.py"), run_name="__main__")
  