#!/usr/bin/env python3
from common import *
target=os.sys.argv[1] if len(os.sys.argv)>1 else "unspecified"
emit(False,target=target,error="failover_requires_current_lease_holder_observation",safe_default="no_failover")
raise SystemExit(2)
