#!/usr/bin/env python3
from common import *
files={p: (PACKAGE/p).is_file() for p in ["project.yaml","compose.yaml","runtime/lease.py","maintenance/maintenance.py"]}
compose=None
if shutil.which("docker"):
 compose=run(["docker","compose","-f",str(PACKAGE/"compose.yaml"),"ps","--format","json"],30)
emit(True,package_files=files,compose=compose)
