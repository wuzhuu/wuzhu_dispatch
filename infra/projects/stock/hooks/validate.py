#!/usr/bin/env python3
from common import *
checks={
 "descriptor": (PACKAGE/"project.yaml").is_file(),
 "dockerfile": (PACKAGE/"Dockerfile").is_file(),
 "compose": (PACKAGE/"compose.yaml").is_file(),
 "lease": (PACKAGE/"runtime/lease.py").is_file(),
 "maintenance": (PACKAGE/"maintenance/maintenance.py").is_file(),
 "no_host_stock_user_contract": True,
 "secrets_not_in_image_manifest": not any((PACKAGE/p).exists() for p in ["key/api_keys.yaml",".env"]),
}
if shutil.which("docker"):
 checks["compose_config"] = run(["docker","compose","-f",str(PACKAGE/"compose.yaml"),"config","--quiet"],30)["success"]
else: checks["compose_config"]="not_run_docker_unavailable"
emit(all(v is True or isinstance(v,str) for v in checks.values()),checks=checks)
