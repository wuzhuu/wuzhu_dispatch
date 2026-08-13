#!/usr/bin/env python3
from common import *
node=os.sys.argv[1] if len(os.sys.argv)>1 else "unspecified"
required=["project.yaml","Dockerfile","compose.yaml","entrypoint.sh","hooks/health.py","maintenance/maintenance.py","runtime/lease.py"]
missing=[p for p in required if not (PACKAGE/p).exists()]
emit(not missing,node=node,missing=missing,host_account_contract=False,requires={"docker":True,"persistent_storage":True,"external_secrets":True})
