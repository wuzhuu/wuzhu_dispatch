#!/usr/bin/env python3
from common import *
source=os.sys.argv[1] if len(os.sys.argv)>1 else "unspecified"; target=os.sys.argv[2] if len(os.sys.argv)>2 else "unspecified"
emit(False,source=source,target=target,error="migration_requires_authenticated_target_and_data_transfer_verification",safe_default="no_cutover")
raise SystemExit(2)
