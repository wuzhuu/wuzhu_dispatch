#!/usr/bin/env python3
from common import *
emit(True,rollback_artifacts=["/etc/hermes-stock/stock-crontab.pre-migration-20260810T191059Z","/mnt/data/stock-migration-backups/20260810T230505Z"],action="restore_previous_release_then_validate")
