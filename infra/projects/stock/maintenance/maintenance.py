#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, pathlib, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
DATA=pathlib.Path(os.getenv("STOCK_DATA_ROOT","/var/lib/stock/data")); QUEUE=pathlib.Path(os.getenv("STOCK_QUEUE_ROOT","/var/lib/stock/queue")); LEASE=pathlib.Path(os.getenv("STOCK_LEASE_ROOT","/var/lib/stock/lease"))
def status():
 return {"project":"stock","data_root":str(DATA),"data_exists":DATA.exists(),"queue_exists":QUEUE.exists(),"pending_uploads":len(list(QUEUE.glob("*.pending"))) if QUEUE.exists() else None,"lease_exists":LEASE.exists(),"mode":os.getenv("STOCK_MODE","standby"),"mailbox":"stock-ops@31411414.xyz"}
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path not in ("/health","/status"): self.send_response(404); self.end_headers(); return
  body=json.dumps(status(),ensure_ascii=False).encode(); self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
 def log_message(self,*a): pass
def main():
 ap=argparse.ArgumentParser(); ap.add_argument("--listen"); args=ap.parse_args()
 host,port=(args.listen or "127.0.0.1:8081").rsplit(":",1); print(json.dumps(status(),ensure_ascii=False))
 HTTPServer((host,int(port)),H).serve_forever()
if __name__=="__main__": main()
