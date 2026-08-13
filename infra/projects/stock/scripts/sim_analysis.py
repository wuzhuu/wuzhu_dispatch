#!/usr/bin/env python3
"""PI2 功分器 EMX 仿真结果批量统计。

扫描 /mnt/data/RFIC_IPD_sim/results/job_*/job_*/:
- 解析 design_params.json (设计参数)
- 解析 *.s3p (Touchstone 3端口 S参数, # Hz S RI R 50)
- 计算指标: 差损(IL) / 隔离(Isolation) / 回波(ReturnLoss) / 平坦度(Flatness)
- 输出: CSV 明细 + JSON 分布统计 + PNG 分布直方图
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

RESULTS_ROOT = Path("/mnt/data/RFIC_IPD_sim/results")


def parse_s3p(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """返回 (freqs[51], S[51,3,3])，S 为复数。失败返回 None。"""
    freqs: list[float] = []
    rows: list[list[complex]] = []
    with open(path) as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("!") and not ln.startswith("#")]
    # 每频点 3 行，每行 6 个数（3 个复数：S[port][0..2]）
    if len(lines) % 3 != 0:
        return None
    for i in range(0, len(lines), 3):
        parts = [p for p in lines[i].split()]
        freq = float(parts[0])
        if i + 3 > len(lines):
            break
        row1 = [complex(float(lines[i].split()[j]), float(lines[i].split()[j + 1])) for j in range(1, 7, 2)]
        row2 = [complex(float(lines[i + 1].split()[j]), float(lines[i + 1].split()[j + 1])) for j in range(0, 6, 2)]
        row3 = [complex(float(lines[i + 2].split()[j]), float(lines[i + 2].split()[j + 1])) for j in range(0, 6, 2)]
        freqs.append(freq)
        rows.append([row1, row2, row3])
    if not freqs:
        return None
    return np.array(freqs), np.array(rows)


def db(x: float) -> float:
    """线性幅度 -> dB。x<=0 时给 -200dB（保护）。"""
    return 20 * math.log10(x) if x > 1e-12 else -200.0


def compute_metrics(S: np.ndarray, freqs: np.ndarray) -> dict[str, float]:
    """S: [N,3,3] 复数。功分器：1 输入，2/3 输出。

    - IL (差损): 平均(|S21|, |S31|) 的 dB（理想 -3.01dB）
    - 隔离: |S23| dB（越小越好）
    - 回波: max(|S11|, |S22|, |S33|) dB（越小越好，用最大=最差）
    - 平坦度: 带内 IL 的 max-min（dB 波动）
    """
    mag = np.abs(S)  # [N,3,3]
    # 频带: 取整个扫频范围 2-6GHz (或可配置)
    il_lin = (mag[:, 0, 1] + mag[:, 0, 2]) / 2.0
    il_db = np.array([db(x) for x in il_lin])
    iso_lin = mag[:, 1, 2]
    iso_db = np.array([db(x) for x in iso_lin])
    rl_lin = np.maximum(np.maximum(mag[:, 0, 0], mag[:, 1, 1]), mag[:, 2, 2])
    rl_db = np.array([db(x) for x in rl_lin])
    il_mean = float(np.mean(il_db))
    iso_mean = float(np.mean(iso_db))
    rl_max = float(np.max(rl_db))  # 最差回波
    flatness = float(np.max(il_db) - np.min(il_db))
    return {
        "il_mean_db": il_mean,
        "il_max_db": float(np.max(il_db)),
        "il_min_db": float(np.min(il_db)),
        "iso_mean_db": iso_mean,
        "iso_max_db": float(np.max(iso_db)),
        "rl_max_db": rl_max,
        "rl_min_db": float(np.min(rl_db)),
        "flatness_db": flatness,
    }


def summarize(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    a = np.array(values)
    q = np.percentile(a, [1, 5, 25, 50, 75, 95, 99])
    return {
        "count": int(len(a)),
        "mean": round(float(a.mean()), 3),
        "std": round(float(a.std()), 3),
        "min": round(float(a.min()), 3),
        "p1": round(float(q[0]), 3),
        "p5": round(float(q[1]), 3),
        "p25": round(float(q[2]), 3),
        "median": round(float(q[3]), 3),
        "p75": round(float(q[4]), 3),
        "p95": round(float(q[5]), 3),
        "p99": round(float(q[6]), 3),
        "max": round(float(a.max()), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(RESULTS_ROOT))
    ap.add_argument("--out", default="/mnt/data/RFIC_IPD_sim/analysis")
    args = ap.parse_args()

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    errors: list[str] = []
    param_names: set[str] = set()

    jobs = sorted([p for p in root.glob("job_*") if p.is_dir()])
    print(f"扫描 {len(jobs)} 个结果目录 ...")

    for job_dir in jobs:
        inner = job_dir / job_dir.name
        if not inner.is_dir():
            # 有些结果直接平铺在 job_XXX/
            inner = job_dir
        s3p_files = sorted(inner.glob("*.s3p"))
        json_files = sorted(inner.glob("design_params.json"))
        if not s3p_files:
            errors.append(f"{job_dir.name}: no s3p")
            continue
        if not json_files:
            errors.append(f"{job_dir.name}: no design_params.json")
            continue

        parsed = parse_s3p(s3p_files[0])
        if parsed is None:
            errors.append(f"{job_dir.name}: s3p parse failed")
            continue
        freqs, S = parsed

        with open(json_files[0]) as f:
            dp = json.load(f)
        params = dp.get("parameters", {})
        param_names.update(params.keys())

        metrics = compute_metrics(S, freqs)
        row = {
            "job": job_dir.name,
            "geometry_id": dp.get("geometry_id", ""),
            "simulation_id": dp.get("simulation_id", ""),
            "dataset_id": dp.get("dataset_id", ""),
            "sampling_region": params.get("sampling_region", ""),
            **metrics,
            **params,
        }
        results.append(row)

    print(f"成功解析: {len(results)} 个, 失败: {len(errors)}")
    if errors:
        print("失败示例:", errors[:5])

    # 输出 CSV
    import csv
    csv_path = out / "simulation_metrics.csv"
    fieldnames = sorted(set().union(*(r.keys() for r in results)))
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"CSV: {csv_path} ({len(results)} 行)")

    # 分布统计
    metric_keys = ["il_mean_db", "il_max_db", "il_min_db", "iso_mean_db",
                   "iso_max_db", "rl_max_db", "rl_min_db", "flatness_db"]
    dist: dict[str, dict] = {}
    for k in metric_keys:
        vals = [r[k] for r in results if isinstance(r.get(k), (int, float))]
        dist[k] = summarize(vals)
    dist["_meta"] = {
        "total_jobs": len(jobs),
        "parsed": len(results),
        "failed": len(errors),
        "freq_ghz_min": round(float(freqs[0]) / 1e9, 2) if len(freqs) else None,
        "freq_ghz_max": round(float(freqs[-1]) / 1e9, 2) if len(freqs) else None,
        "freq_points": int(len(freqs)) if len(freqs) else 0,
    }
    dist_json = out / "distribution_stats.json"
    with open(dist_json, "w") as f:
        json.dump(dist, f, indent=2, ensure_ascii=False)
    print(f"JSON: {dist_json}")

    # 设计参数统计
    numeric_params = [k for k in param_names if k not in ("sampling_region", "strict_symmetry")]
    param_dist: dict[str, dict] = {}
    for k in numeric_params:
        vals = [r[k] for r in results if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
        if vals:
            param_dist[k] = summarize(vals)
    with open(out / "design_param_stats.json", "w") as f:
        json.dump(param_dist, f, indent=2, ensure_ascii=False)

    # 直方图
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        n = len(metric_keys)
        fig, axes = plt.subplots(2, 4, figsize=(20, 9))
        for ax, k in zip(axes.flat, metric_keys):
            vals = [r[k] for r in results if isinstance(r.get(k), (int, float))]
            ax.hist(vals, bins=50, color="#4C72B0", alpha=0.8, edgecolor="white")
            ax.set_title(f"{k}  (n={len(vals)})", fontsize=11)
            ax.set_xlabel("dB")
            ax.set_ylabel("count")
            ax.grid(alpha=0.3)
        fig.suptitle(f"PI2 Power Divider EMX Metric Distributions (n={len(results)})", fontsize=14)
        fig.tight_layout(rect=[0, 0, 1, 0.96])
        fig.savefig(out / "metric_distributions.png", dpi=110)
        print(f"PNG: {out / 'metric_distributions.png'}")
    except Exception as e:
        print(f"图表生成失败: {e}")

    # 控制台摘要
    print("\n=== 指标分布汇总 ===")
    for k in metric_keys:
        s = dist[k]
        print(f"{k:14s} n={s['count']:5d} mean={s['mean']:8.3f} std={s['std']:7.3f} "
              f"min={s['min']:8.3f} p25={s['p25']:8.3f} med={s['median']:8.3f} p75={s['p75']:8.3f} max={s['max']:8.3f}")


if __name__ == "__main__":
    main()
