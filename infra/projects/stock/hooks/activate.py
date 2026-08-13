#!/usr/bin/env python3
from common import *
node=os.sys.argv[1] if len(os.sys.argv)>1 else "unspecified"
emit(False,node=node,error="activation_requires_central_lease_and_authenticated_target",safe_default="not_activated")
raise SystemExit(2)
