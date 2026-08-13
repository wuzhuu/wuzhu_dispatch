#!/usr/bin/env python3
from common import *
checks={"package":PACKAGE.exists(),"lease":(PACKAGE/"runtime/lease.py").is_file(),"maintenance":(PACKAGE/"maintenance/maintenance.py").is_file()}
emit(all(checks.values()),checks=checks,readiness="READY" if all(checks.values()) else "NOT_READY")
