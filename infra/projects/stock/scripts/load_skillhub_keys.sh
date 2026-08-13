#!/bin/bash
# 从 stock 项目 key 文件加载 API 密钥到环境变量
# 供 shell 和 Hermes 子进程使用

KEY_FILE="$HOME/stock/key/api_keys.yaml"
if [ -f "$KEY_FILE" ]; then
  # 使用 python3 解析 yaml 并导出 env
  eval "$(python3 -c "
import yaml, os
with open(os.path.expanduser('$KEY_FILE')) as f:
    k = yaml.safe_load(f)
ek = k.get('equal_data', {})
if ek.get('enabled') and ek.get('api_key'):
    print(f'export EQUAL_DATA_API_KEY=\"{ek[\"api_key\"]}\"')
lk = k.get('lhb_api', {})
if lk.get('enabled') and lk.get('api_key'):
    print(f'export LHB_API_KEY=\"{lk[\"api_key\"]}\"')
    print(f'export LHB_API_URL=\"{lk.get(\"base_url\",\"http://fffy520.gicp.net:8003\")}\"')
")"
fi
