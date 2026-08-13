#!/usr/bin/env python3
from common import *
import yaml
data=yaml.safe_load((PACKAGE/"project.yaml").read_text())
emit(True,project_id=data.get("project_id"),artifact=data.get("artifact"),runtime=data.get("runtime"),production_readiness=data.get("production_readiness"))
