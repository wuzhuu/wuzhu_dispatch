#!/usr/bin/env python3
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, time
ROOT=pathlib.Path(os.environ.get("HERMES_PROJECT_DIR", pathlib.Path(__file__).resolve().parents[1]))
PACKAGE=ROOT

def emit(success=True, **details):
 print(json.dumps({"success": success, "project_id": os.environ.get("HERMES_PROJECT_ID","stock"), "hook": pathlib.Path(__file__).stem, "timestamp": time.time(), "details": details}, ensure_ascii=False, sort_keys=True))

def run(argv, timeout=60):
 try:
  p=subprocess.run(argv,cwd=PACKAGE,text=True,capture_output=True,timeout=timeout)
  return {"argv":argv,"returncode":p.returncode,"stdout":p.stdout[-3000:],"stderr":p.stderr[-3000:],"success":p.returncode==0}
 except FileNotFoundError:
  return {"argv":argv,"success":False,"error":"command_not_found"}
 except subprocess.TimeoutExpired:
  return {"argv":argv,"success":False,"error":"timeout"}
