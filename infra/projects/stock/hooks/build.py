#!/usr/bin/env python3
from common import *
if not shutil.which("docker"):
 emit(False,error="docker_not_available"); raise SystemExit(2)
r=run(["docker","build","-t","hermes-stock:portable","."],900)
emit(r["success"],build=r,image="hermes-stock:portable")
raise SystemExit(0 if r["success"] else 2)
