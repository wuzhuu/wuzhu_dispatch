#!/usr/bin/env python3
"""
SkillHub 技能统一调用脚本
用法:
  python3 skillhub_client.py westock search 宁德时代
  python3 skillhub_client.py equal B6 --period 1 --pageSize 5
  python3 skillhub_client.py lhb daily [2026-07-03]
  python3 skillhub_client.py lhb history 002031
"""
import sys, os, json, yaml

# 加载 Key
KEY_FILE = os.path.expanduser("~/stock/key/api_keys.yaml")
with open(KEY_FILE) as f:
    KEYS = yaml.safe_load(f)

def export_env():
    """导出环境变量（供子进程使用）"""
    env = os.environ.copy()
    ek = KEYS.get("equal_data", {})
    if ek.get("enabled"):
        env["EQUAL_DATA_API_KEY"] = ek["api_key"]
    lk = KEYS.get("lhb_api", {})
    if lk.get("enabled"):
        env["LHB_API_KEY"] = lk["api_key"]
        env["LHB_API_URL"] = lk.get("base_url", "http://fffy520.gicp.net:8003")
    return env

def cmd_westock(args):
    """westock-data: 通过 npx 运行"""
    import subprocess
    cmd = ["npx", "-y", "westock-data-skillhub@1.0.5"] + list(args)
    env = export_env()
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    print(r.stdout)
    if r.stderr and "npm notice" not in r.stderr:
        print(r.stderr, file=sys.stderr)
    return r.returncode

def cmd_equal(args):
    """equal-data: 查询金融数据"""
    import argparse
    from equal_data import EqualDataApi
    parser = argparse.ArgumentParser()
    parser.add_argument("interface", help="接口ID (如 B6=高管增减持)")
    parser.add_argument("--period", type=int, default=1)
    parser.add_argument("--pageSize", type=int, default=10)
    parser.add_argument("--pageIndex", type=int, default=0)
    ns, _ = parser.parse_known_args(args)
    api = EqualDataApi(KEYS["equal_data"]["api_key"])
    data = api.query_equal_data(
        interfaceId=ns.interface, period=ns.period,
        pageIndex=ns.pageIndex, pageSize=ns.pageSize
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0

def cmd_lhb(args):
    """龙虎榜数据"""
    import urllib.request
    if not args or args[0] == "daily":
        date = args[1] if len(args) > 1 else None
        path = "/api/lhb/daily" + (f"?date={date}" if date else "")
    elif args[0] == "history":
        path = f"/api/lhb/history?code={args[1]}" if len(args) > 1 else "/api/lhb/history"
    else:
        print(f"未知命令: {args[0]}")
        return 1
    url = KEYS["lhb_api"]["base_url"].rstrip("/") + path
    req = urllib.request.Request(url)
    api_key = KEYS["lhb_api"]["api_key"]
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    skill = sys.argv[1]
    args = sys.argv[2:]
    cmds = {"westock": cmd_westock, "equal": cmd_equal, "lhb": cmd_lhb}
    if skill not in cmds:
        print(f"未知技能: {skill}，可用: {', '.join(cmds.keys())}")
        sys.exit(1)
    sys.exit(cmds[skill](args))

if __name__ == "__main__":
    main()
