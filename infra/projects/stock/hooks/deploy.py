#!/usr/bin/env python3
from common import *
node=os.sys.argv[1] if len(os.sys.argv)>1 else "unspecified"
emit(True,mode="descriptor_only",node=node,action_required="authenticated_worker_deploy",rollback="previous_release_retained")
