#!/usr/bin/env python3
"""PI2 功分器 EMX 仿真结果 — 完整统计报告生成。

输入: /mnt/data/RFIC_IPD_sim/results/job_*/job_*/*.s3p + design_params.json
输出: /mnt/data/RFIC_IPD_sim/analysis/report/
  - fig_01_metrics_histograms.png   8 指标直方图
  - fig_02_metrics_boxes.png        8 指标箱线图
  - fig_03_metrics_cdf.png          8 指标 CDF
  - fig_04_frequency_response.png   S11/S21/S31/S23 中位数+5~95% 频带
  - fig_05_input_dims.png           输入 11 维度分布（电阻 a/b 统一）
  - fig_06_metric_corr.png          8 指标相关性热图
  - fig_07_metric_scatter.png       4 核心指标两两散点
  - fig_08_resistor_ab.png          电阻 a/b 分布与比值
  - report.md                       完整统计报告
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

RESULTS_ROOT = Path("/mnt/data/RFIC_IPD_sim/results")
OUT = Path("/mnt/data/RFIC_IPD_sim/analysis/report")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

METRICS = {
    "il_mean_db": "Insertion Loss (mean)",
    "il_max_db": "Insertion Loss (max)",
    "il_min_db": "Insertion Loss (min)",
    "iso_mean_db": "Isolation (mean)",
    "iso_max_db": "Isolation (worst)",
    "rl_max_db": "Return Loss (worst)",
    "rl_min_db": "Return Loss (best)",
    "flatness_db": "Flatness (ripple)",
}
CORE_METRICS = ["il_mean_db", "iso_mean_db", "rl_max_db", "flatness_db"]


def parse_s3p(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    with open(path) as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith(("!", "#"))]
    if len(lines) % 3 != 0:
        return None
    freqs: list[float] = []
    rows: list[list[list[complex]]] = []
    for i in range(0, len(lines), 3):
        parts = lines[i].split()
        freqs.append(float(parts[0]))
        r1 = [complex(float(parts[j]), float(parts[j + 1])) for j in range(1, 7, 2)]
        r2p = lines[i + 1].split()
        r2 = [complex(float(r2p[j]), float(r2p[j + 1])) for j in range(0, 6, 2)]
        r3p = lines[i + 2].split()
        r3 = [complex(float(r3p[j]), float(r3p[j + 1])) for j in range(0, 6, 2)]
        rows.append([r1, r2, r3])
    if not freqs:
        return None
    return np.array(freqs), np.array(rows)


def db(x: float) -> float:
    return 20 * math.log10(x) if x > 1e-12 else -200.0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # ---------- 1. 扫描并解析全部结果 ----------
    jobs = sorted([p for p in RESULTS_ROOT.glob("job_*") if p.is_dir()])
    records: list[dict] = []
    freq_all = None
    trace_arrays: dict[str, list[np.ndarray]] = {k: [] for k in ("s11", "s21", "s31", "s23")}

    for job_dir in jobs:
        inner = job_dir / job_dir.name
        if not inner.is_dir():
            inner = job_dir
        s3p_files = sorted(inner.glob("*.s3p"))
        json_files = sorted(inner.glob("design_params.json"))
        if not s3p_files or not json_files:
            continue
        parsed = parse_s3p(s3p_files[0])
        if parsed is None:
            continue
        freqs, S = parsed
        if freq_all is None:
            freq_all = freqs
        try:
            with open(json_files[0]) as f:
                dp = json.load(f)
        except Exception:
            continue
        params = dp.get("parameters", {})
        mag = np.abs(S)
        for name, (i, j) in {"s11": (0, 0), "s21": (1, 0), "s31": (2, 0), "s23": (1, 2)}.items():
            trace_arrays[name].append(np.array([db(x) for x in mag[:, i, j]]))
        # 指标
        il_db = np.array([db(x) for x in (mag[:, 0, 1] + mag[:, 0, 2]) / 2.0])
        iso_db = np.array([db(x) for x in mag[:, 1, 2]])
        rl_db = np.array([db(x) for x in np.maximum(np.maximum(mag[:, 0, 0], mag[:, 1, 1]), mag[:, 2, 2])])
        rec = {
            "job": job_dir.name,
            "il_mean_db": float(np.mean(il_db)),
            "il_max_db": float(np.max(il_db)),
            "il_min_db": float(np.min(il_db)),
            "iso_mean_db": float(np.mean(iso_db)),
            "iso_max_db": float(np.max(iso_db)),
            "rl_max_db": float(np.max(rl_db)),
            "rl_min_db": float(np.min(rl_db)),
            "flatness_db": float(np.max(il_db) - np.min(il_db)),
        }
        rec.update(params)
        records.append(rec)

    n = len(records)
    print(f"解析成功: {n} 个结果, 频点 {len(freq_all)} ({freq_all[0]/1e9:.2f}-{freq_all[-1]/1e9:.2f} GHz)")
    if n == 0:
        print("无数据，退出")
        return

    # ---------- 2. 指标直方图 ----------
    fig, axes = plt.subplots(2, 4, figsize=(20, 9))
    for ax, k in zip(axes.flat, METRICS):
        vals = [r[k] for r in records]
        ax.hist(vals, bins=60, color="#4C72B0", alpha=0.85, edgecolor="white")
        ax.set_title(f"{METRICS[k]}\n{n} samples", fontsize=10)
        ax.set_xlabel("dB")
        ax.grid(alpha=0.3)
    fig.suptitle("PI2 Power Divider EMX — Metric Histograms", fontsize=15)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(OUT / "fig_01_metrics_histograms.png", dpi=110)
    plt.close(fig)

    # ---------- 3. 指标箱线图 ----------
    fig, axes = plt.subplots(1, 8, figsize=(24, 5))
    for ax, k in zip(axes, METRICS):
        vals = [r[k] for r in records]
        bp = ax.boxplot(vals, vert=True, patch_artist=True, showfliers=True,
                        flierprops=dict(marker=".", markersize=2, alpha=0.3))
        bp["boxes"][0].set_facecolor("#4C72B0")
        bp["boxes"][0].set_alpha(0.7)
        ax.set_title(METRICS[k].split("(")[0].strip(), fontsize=9)
        ax.set_ylabel("dB")
        ax.grid(alpha=0.3)
    fig.suptitle("PI2 Power Divider EMX — Metric Box Plots (outliers shown as dots)", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.92])
    fig.savefig(OUT / "fig_02_metrics_boxes.png", dpi=110)
    plt.close(fig)

    # ---------- 4. 指标 CDF ----------
    fig, axes = plt.subplots(2, 4, figsize=(20, 9))
    for ax, k in zip(axes.flat, METRICS):
        vals = np.sort(np.array([r[k] for r in records]))
        cdf = np.arange(1, len(vals) + 1) / len(vals)
        ax.plot(vals, cdf, color="#4C72B0", lw=1.6)
        ax.axvline(np.median(vals), color="r", ls="--", lw=1, label=f"median={np.median(vals):.2f}")
        ax.set_title(METRICS[k], fontsize=10)
        ax.set_xlabel("dB")
        ax.set_ylabel("CDF")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
    fig.suptitle("PI2 Power Divider EMX — Metric CDFs", fontsize=15)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(OUT / "fig_03_metrics_cdf.png", dpi=110)
    plt.close(fig)

    # ---------- 5. 频率响应曲线（中位数 + 5~95% 带）----------
    fghz = freq_all / 1e9
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    for ax, (name, label) in zip(axes.flat, [
        ("s11", "S11 (input return loss)"),
        ("s21", "S21 (output 1, insertion loss)"),
        ("s31", "S31 (output 2, insertion loss)"),
        ("s23", "S23 (isolation)"),
    ]):
        arr = np.array(trace_arrays[name])  # [N, 51]
        med = np.median(arr, axis=0)
        lo = np.percentile(arr, 5, axis=0)
        hi = np.percentile(arr, 95, axis=0)
        # 抽样画细线（最多 400 条）
        step = max(1, arr.shape[0] // 400)
        for row in arr[::step]:
            ax.plot(fghz, row, color="#9ecae1", lw=0.4, alpha=0.35)
        ax.fill_between(fghz, lo, hi, color="#4C72B0", alpha=0.25, label="5–95% band")
        ax.plot(fghz, med, color="#1f3b6e", lw=2.2, label="median")
        ax.set_title(f"{label} (n={n})", fontsize=11)
        ax.set_xlabel("Frequency (GHz)")
        ax.set_ylabel("Magnitude (dB)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=9)
    fig.suptitle("PI2 Power Divider EMX — Frequency Response (all samples, median + 5–95% band)", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(OUT / "fig_04_frequency_response.png", dpi=110)
    plt.close(fig)

    # ---------- 6. 输入 11 维度分布（电阻 a/b 统一）----------
    # 维度定义：电感 7 + 电容 3 + 电阻 1 组（a=R1a∪R2a, b=R1b∪R2b）
    dims = [
        ("stage1_turns", "L1 turns", "count"),
        ("stage1_r_um", "L1 r (um)", "cont"),
        ("stage1_d_um", "L1 d (um)", "cont"),
        ("stage2_turns", "L2 turns", "count"),
        ("stage2_r_um", "L2 r (um)", "cont"),
        ("stage2_d_um", "L2 d (um)", "cont"),
        ("common_w_um", "W (um)", "cont"),
        ("c1_l_um", "C1 l (um)", "cont"),
        ("c2_l_um", "C2 l (um)", "cont"),
        ("c3_l_um", "C3 l (um)", "cont"),
        ("RESISTOR", "R1/R2 a/b (um)", "special"),
    ]
    fig, axes = plt.subplots(3, 4, figsize=(22, 12))
    axes = axes.flat
    for ax, (key, title, kind) in zip(axes, dims):
        if kind == "count":
            vals = [r[key] for r in records if r.get(key) is not None]
            from collections import Counter
            cnt = Counter(vals)
            labels = sorted(cnt.keys())
            ax.bar([str(x) for x in labels], [cnt[x] for x in labels], color="#4C72B0", alpha=0.85)
            ax.set_title(f"{title} (discrete)", fontsize=10)
            ax.set_xlabel("value")
            ax.set_ylabel("count")
        elif kind == "special":
            a_vals = [r["r1_a_um"] for r in records] + [r["r2_a_um"] for r in records]
            b_vals = [r["r1_b_um"] for r in records] + [r["r2_b_um"] for r in records]
            ax.hist(a_vals, bins=60, color="#c44e52", alpha=0.7, label=f"a (n={len(a_vals)})")
            ax.hist(b_vals, bins=60, color="#4C72B0", alpha=0.7, label=f"b (n={len(b_vals)})")
            ax.set_title("Resistor a/b (unified R1+R2)", fontsize=10)
            ax.set_xlabel("um")
            ax.set_ylabel("count")
            ax.legend(fontsize=8)
        else:
            vals = [r[key] for r in records if r.get(key) is not None]
            ax.hist(vals, bins=60, color="#4C72B0", alpha=0.85, edgecolor="white")
            ax.set_title(f"{title}", fontsize=10)
            ax.set_xlabel("um")
            ax.set_ylabel("count")
        ax.grid(alpha=0.3)
    axes[-1].axis("off")
    fig.suptitle("PI2 Input Parameter Distributions (11 dims; resistor R1/R2 unified to a/b)", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(OUT / "fig_05_input_dims.png", dpi=110)
    plt.close(fig)

    # ---------- 7. 指标相关性热图 ----------
    keys = list(METRICS.keys())
    M = np.array([[r[k] for k in keys] for r in records])
    corr = np.corrcoef(M.T)
    fig, ax = plt.subplots(figsize=(11, 9))
    im = ax.imshow(corr, cmap="coolwarm", vmin=-1, vmax=1)
    ax.set_xticks(range(len(keys)))
    ax.set_yticks(range(len(keys)))
    short = [k.replace("_db", "") for k in keys]
    ax.set_xticklabels(short, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(short, fontsize=8)
    for i in range(len(keys)):
        for j in range(len(keys)):
            ax.text(j, i, f"{corr[i, j]:.2f}", ha="center", va="center", fontsize=7,
                    color="white" if abs(corr[i, j]) > 0.5 else "black")
    fig.colorbar(im, ax=ax, shrink=0.8)
    ax.set_title("Metric Correlation Matrix", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT / "fig_06_metric_corr.png", dpi=110)
    plt.close(fig)

    # ---------- 8. 4 核心指标两两散点 ----------
    fig, axes = plt.subplots(4, 4, figsize=(16, 14))
    labels = [METRICS[k].split("(")[0].strip() for k in CORE_METRICS]
    for i in range(4):
        for j in range(4):
            ax = axes[i, j]
            ki, kj = CORE_METRICS[i], CORE_METRICS[j]
            xi = [r[ki] for r in records]
            xj = [r[kj] for r in records]
            if i == j:
                ax.hist(xi, bins=60, color="#4C72B0", alpha=0.8)
                ax.set_title(f"n={len(xi)}", fontsize=9)
            else:
                ax.scatter(xj, xi, s=3, alpha=0.25, color="#4C72B0", rasterized=True)
                if i == 3:
                    ax.set_xlabel(labels[j], fontsize=9)
                if j == 0:
                    ax.set_ylabel(labels[i], fontsize=9)
            ax.grid(alpha=0.3)
    fig.suptitle("PI2 Core Metrics Pairwise (IL / Isolation / ReturnLoss / Flatness)", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(OUT / "fig_07_metric_scatter.png", dpi=110)
    plt.close(fig)

    # ---------- 9. 电阻 a/b 详细分布 ----------
    r1a = [r["r1_a_um"] for r in records]
    r1b = [r["r1_b_um"] for r in records]
    r2a = [r["r2_a_um"] for r in records]
    r2b = [r["r2_b_um"] for r in records]
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    for ax, (vals, t) in zip(axes.flat, [
        (r1a, "R1 a (length, um)"), (r1b, "R1 b (width, um)"),
        (r2a, "R2 a (length, um)"), (r2b, "R2 b (width, um)"),
    ]):
        ax.hist(vals, bins=60, color="#c44e52" if "a " in t or "a (" in t else "#4C72B0", alpha=0.85)
        ax.set_title(t, fontsize=11)
        ax.set_xlabel("um")
        ax.grid(alpha=0.3)
    fig.suptitle("Resistor Geometry: R1/R2 length(a) & width(b)", fontsize=13)
    fig.tight_layout()
    fig.savefig(OUT / "fig_08_resistor_ab.png", dpi=110)
    plt.close(fig)

    # ---------- 10. 分布统计 JSON ----------
    def summarize(vals: list[float]) -> dict:
        a = np.array(vals)
        q = np.percentile(a, [1, 5, 25, 50, 75, 95, 99])
        return {
            "count": int(len(a)), "mean": round(float(a.mean()), 3), "std": round(float(a.std()), 3),
            "min": round(float(a.min()), 3), "p1": round(float(q[0]), 3), "p5": round(float(q[1]), 3),
            "p25": round(float(q[2]), 3), "median": round(float(q[3]), 3), "p75": round(float(q[4]), 3),
            "p95": round(float(q[5]), 3), "p99": round(float(q[6]), 3), "max": round(float(a.max()), 3),
        }

    stats = {"metrics": {k: summarize([r[k] for r in records]) for k in METRICS}}
    # 11 维输入分布
    dim_stats = {}
    for key, title, kind in dims:
        if kind == "count":
            vals = [r[key] for r in records if r.get(key) is not None]
            from collections import Counter
            dim_stats[key] = {"kind": "discrete", "counts": dict(Counter(vals))}
        elif kind == "special":
            a_vals = [r["r1_a_um"] for r in records] + [r["r2_a_um"] for r in records]
            b_vals = [r["r1_b_um"] for r in records] + [r["r2_b_um"] for r in records]
            dim_stats["resistor_a"] = summarize(a_vals)
            dim_stats["resistor_b"] = summarize(b_vals)
        else:
            dim_stats[key] = summarize([r[key] for r in records if r.get(key) is not None])
    stats["input_dims"] = dim_stats
    stats["meta"] = {
        "n": n,
        "freq_ghz": [round(float(freq_all[0]) / 1e9, 2), round(float(freq_all[-1]) / 1e9, 2)],
        "freq_points": int(len(freq_all)),
    }
    with open(OUT / "report_stats.json", "w") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    print("JSON: report_stats.json")

    # ---------- 11. Markdown 报告 ----------
    def pct_line(name: str, s: dict) -> str:
        return (f"| {name} | {s['count']} | {s['mean']:.2f} | {s['std']:.2f} | {s['min']:.2f} "
                f"| {s['p25']:.2f} | {s['median']:.2f} | {s['p75']:.2f} | {s['max']:.2f} |")

    SHORT = {
        "il_mean_db": "IL (mean)",
        "il_max_db": "IL (best in-band)",
        "il_min_db": "IL (worst in-band)",
        "iso_mean_db": "Isolation (mean)",
        "iso_max_db": "Isolation (worst)",
        "rl_max_db": "ReturnLoss (worst)",
        "rl_min_db": "ReturnLoss (best)",
        "flatness_db": "Flatness (ripple)",
    }

    lines = []
    lines.append("# PI2 功分器 EMX 仿真数据收集统计报告\n")
    lines.append(f"**统计时间**: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    lines.append(f"**样本数**: {n} 个完整结果（含 s3p + design_params）  ")
    lines.append(f"**频率范围**: {freq_all[0]/1e9:.2f}–{freq_all[-1]/1e9:.2f} GHz，{len(freq_all)} 个频点  ")
    lines.append(f"**数据来源**: {RESULTS_ROOT}\n")
    lines.append("## 1. 指标定义\n")
    lines.append("| 指标 | 定义 |")
    lines.append("|---|---|")
    lines.append("| 差损 IL | 平均(\\|S21\\|, \\|S31\\|) 的 dB，理想功分器 = −3.01 dB |")
    lines.append("| 隔离 Isolation | \\|S23\\| 的 dB，越小越好 |")
    lines.append("| 回波 Return Loss | max(\\|S11\\|, \\|S22\\|, \\|S33\\|) dB（取最差端口） |")
    lines.append("| 平坦度 Flatness | 带内 IL 最大−最小（dB 波动） |\n")
    lines.append("## 2. 指标分布\n")
    lines.append("| 指标 | n | 均值 | 标准差 | Min | P25 | 中位 | P75 | Max |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k in METRICS:
        lines.append(pct_line(SHORT[k], stats["metrics"][k]))
    lines.append("\n![指标直方图](fig_01_metrics_histograms.png)")
    lines.append("\n![指标箱线图](fig_02_metrics_boxes.png)")
    lines.append("\n![指标CDF](fig_03_metrics_cdf.png)")
    lines.append("\n## 3. 频率响应\n")
    lines.append("![频率响应曲线](fig_04_frequency_response.png)")
    lines.append("\n## 4. 输入参数 11 维度分布\n")
    lines.append("输入维度 = 电感 7（L1/L2 的 turns/r/d + 公共宽度 W）+ 电容 3（C1/C2/C3 边长）+ 电阻 1 组（R1/R2 长宽统一为 a/b）。\n")
    lines.append("![输入维度分布](fig_05_input_dims.png)")
    lines.append("\n### 电阻 a/b（R1/R2 统一）\n")
    lines.append("| 统计 | a (长度, um) | b (宽度, um) |")
    lines.append("|---|---:|---:|")
    for key in ("median", "p25", "p75", "min", "max"):
        sa, sb = stats["input_dims"]["resistor_a"], stats["input_dims"]["resistor_b"]
        lines.append(f"| {key} | {sa[key]:.2f} | {sb[key]:.2f} |")
    lines.append("\n![电阻a/b分布](fig_08_resistor_ab.png)")
    lines.append("\n## 5. 指标相关性\n")
    lines.append("![指标相关性热图](fig_06_metric_corr.png)")
    lines.append("\n## 6. 核心指标两两关系\n")
    lines.append("![核心指标散点](fig_07_metric_scatter.png)")
    with open(OUT / "report.md", "w") as f:
        f.write("\n".join(lines))
    print("Markdown: report.md")
    print(f"输出目录: {OUT}")


if __name__ == "__main__":
    main()
