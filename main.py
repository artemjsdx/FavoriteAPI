import sys
import os

container_dir = os.path.dirname(os.path.abspath(__file__))
if container_dir not in sys.path:
    sys.path.insert(0, container_dir)
os.chdir(container_dir)

import runpy
runpy.run_path(os.path.join(container_dir, "api.py"), run_name="__main__")
