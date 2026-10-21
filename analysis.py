from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats


ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "cookie_cats.csv"
SQL_PATH = ROOT / "sql" / "analysis.sql"
BUILD_DIR = ROOT / ".build"
REPORT_DIR = ROOT / "report"
FIGURE_DIR = ROOT / "figures"

EXPECTED_COLUMNS = [
    "userid",
    "version",
    "sum_gamerounds",
    "retention_1",
    "retention_7",
]
GROUPS = ["gate_30", "gate_40"]
COLORS = {
    "gate_30": "#0072B2",
    "gate_40": "#D55E00",
}
BUCKET_ORDER = ["0", "1-9", "10-29", "30-49", "50-99", "100+"]
Z_95 = 1.959963984540054


def _as_binary(series: pd.Series) -> pd.Series:
    mapping = {
        "true": 1,
        "false": 0,
        "1": 1,
        "0": 0,
    }
    converted = series.astype(str).str.lower().map(mapping)
    if converted.isna().any():
        values = sorted(series.astype(str).unique().tolist())
        raise ValueError(f"Unexpected boolean values: {values}")
    return converted.astype(int)


def load_data(path: Path = DATA_PATH) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [column for column in EXPECTED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = df[EXPECTED_COLUMNS].copy()
    df["userid"] = pd.to_numeric(df["userid"], errors="raise").astype("int64")
    df["sum_gamerounds"] = pd.to_numeric(
        df["sum_gamerounds"], errors="raise"
    ).astype("int64")
    df["retention_1"] = _as_binary(df["retention_1"])
    df["retention_7"] = _as_binary(df["retention_7"])
    df["version"] = df["version"].astype(str).str.strip()
    return df


def validate_data(df: pd.DataFrame) -> dict:
    duplicate_ids = int(df["userid"].duplicated().sum())
    unknown_versions = sorted(set(df["version"]) - set(GROUPS))
    negative_rounds = int((df["sum_gamerounds"] < 0).sum())
    missing_by_column = {
        column: int(count) for column, count in df.isna().sum().items()
    }

    if duplicate_ids:
        raise ValueError(f"Duplicate user IDs found: {duplicate_ids}")
    if unknown_versions:
        raise ValueError(f"Unexpected experiment groups: {unknown_versions}")
    if negative_rounds:
        raise ValueError(f"Negative game rounds found: {negative_rounds}")
    if any(missing_by_column.values()):
        raise ValueError(f"Missing values found: {missing_by_column}")

    counts = df["version"].value_counts().reindex(GROUPS)
    srm = stats.chisquare(counts.to_numpy())
    return {
        "row_count": int(len(df)),
        "unique_users": int(df["userid"].nunique()),
        "duplicate_userids": duplicate_ids,
        "unknown_versions": unknown_versions,
        "negative_game_rounds": negative_rounds,
        "missing_by_column": missing_by_column,
        "group_counts": {group: int(counts[group]) for group in GROUPS},
        "group_shares": {
            group: float(counts[group] / len(df)) for group in GROUPS
        },
        "srm_chi_square": float(srm.statistic),
        "srm_p_value": float(srm.pvalue),
    }


def create_database(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE players (
                userid INTEGER PRIMARY KEY,
                version TEXT NOT NULL,
                sum_gamerounds INTEGER NOT NULL,
                retention_1 INTEGER NOT NULL CHECK (retention_1 IN (0, 1)),
                retention_7 INTEGER NOT NULL CHECK (retention_7 IN (0, 1))
            )
            """
        )
        df.to_sql("players", connection, if_exists="append", index=False)
        connection.commit()


def parse_named_queries(sql_text: str) -> dict[str, str]:
    parts = re.split(r"^-- name:\s*([A-Za-z0-9_]+)\s*$", sql_text, flags=re.M)
    if len(parts) < 3:
        raise ValueError("No named queries found in sql/analysis.sql")

    queries: dict[str, str] = {}
    for index in range(1, len(parts), 2):
        name = parts[index].strip()
        query = parts[index + 1].strip().rstrip(";").strip()
        if query:
            queries[name] = query
    return queries


def run_sql_queries(db_path: Path, sql_path: Path) -> dict[str, pd.DataFrame]:
    queries = parse_named_queries(sql_path.read_text(encoding="utf-8"))
    output: dict[str, pd.DataFrame] = {}
    with sqlite3.connect(db_path) as connection:
        for name, query in queries.items():
            output[name] = pd.read_sql_query(query, connection)
    return output


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    if total <= 0:
        raise ValueError("Total must be positive")
    p = successes / total
    denominator = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))
        / denominator
    )
    return center - margin, center + margin


def compare_proportions(
    control_successes: int,
    control_total: int,
    test_successes: int,
    test_total: int,
) -> dict:
    control_rate = control_successes / control_total
    test_rate = test_successes / test_total
    difference = test_rate - control_rate
    pooled = (control_successes + test_successes) / (
        control_total + test_total
    )
    pooled_se = math.sqrt(
        pooled * (1 - pooled) * (1 / control_total + 1 / test_total)
    )
    unpooled_se = math.sqrt(
        control_rate * (1 - control_rate) / control_total
        + test_rate * (1 - test_rate) / test_total
    )
    z_score = difference / pooled_se if pooled_se else 0.0
    p_value = float(2 * stats.norm.sf(abs(z_score)))
    control_ci = wilson_interval(control_successes, control_total)
    test_ci = wilson_interval(test_successes, test_total)
    return {
        "control_rate": control_rate,
        "test_rate": test_rate,
        "difference": difference,
        "difference_pp": difference * 100,
        "relative_lift_pct": (
            (test_rate / control_rate - 1) * 100 if control_rate else None
        ),
        "difference_ci95": [
            difference - Z_95 * unpooled_se,
            difference + Z_95 * unpooled_se,
        ],
        "difference_ci95_pp": [
            (difference - Z_95 * unpooled_se) * 100,
            (difference + Z_95 * unpooled_se) * 100,
        ],
        "z_score": z_score,
        "p_value": p_value,
        "control_wilson_ci95": list(control_ci),
        "test_wilson_ci95": list(test_ci),
    }


def calculate_round_stats(df: pd.DataFrame) -> dict:
    output: dict[str, dict] = {}
    p99 = float(df["sum_gamerounds"].quantile(0.99))
    for group in GROUPS:
        values = df.loc[df["version"] == group, "sum_gamerounds"]
        winsorized = values.clip(upper=p99)
        output[group] = {
            "users": int(len(values)),
            "mean": float(values.mean()),
            "median": float(values.median()),
            "p95": float(values.quantile(0.95)),
            "p99": float(values.quantile(0.99)),
            "max": int(values.max()),
            "zero_users": int((values == 0).sum()),
            "zero_rate": float((values == 0).mean()),
            "winsorized_mean_p99": float(winsorized.mean()),
        }

    control = df.loc[df["version"] == "gate_30", "sum_gamerounds"]
    test = df.loc[df["version"] == "gate_40", "sum_gamerounds"]
    test_result = stats.mannwhitneyu(control, test, alternative="two-sided")
    return {
        "groups": output,
        "p99_threshold": p99,
        "mann_whitney_u": float(test_result.statistic),
        "mann_whitney_p_value": float(test_result.pvalue),
    }


def calculate_bucket_metrics(df: pd.DataFrame) -> list[dict]:
    frame = df.copy()
    frame["round_bucket"] = pd.cut(
        frame["sum_gamerounds"],
        bins=[-1, 0, 9, 29, 49, 99, np.inf],
        labels=BUCKET_ORDER,
    )
    grouped = (
        frame.groupby(["version", "round_bucket"], observed=False)
        .agg(
            users=("userid", "size"),
            d1_retention=("retention_1", "mean"),
            d7_retention=("retention_7", "mean"),
            median_rounds=("sum_gamerounds", "median"),
        )
        .reset_index()
    )
    return grouped.to_dict(orient="records")


def decide(groups: dict, comparisons: dict) -> dict:
    d1 = comparisons["retention_1"]
    d7 = comparisons["retention_7"]
    negatives = []
    if d7["difference"] < 0 and d7["p_value"] < 0.05:
        negatives.append("D7 留存显著下降")
    if d1["difference"] < 0 and d1["p_value"] < 0.05:
        negatives.append("D1 留存显著下降")

    if negatives:
        recommendation = "保留 gate_30，不上线 gate_40"
        code = "keep_gate_30"
    elif (
        (d7["difference"] > 0 and d7["p_value"] < 0.05)
        or (d1["difference"] > 0 and d1["p_value"] < 0.05)
    ):
        recommendation = "gate_40 在主要指标上未出现负向影响，可进入付费指标复核"
        code = "continue_gate_40_validation"
    else:
        recommendation = "证据不足，建议扩大样本或延长观察后再判断"
        code = "inconclusive"

    return {
        "code": code,
        "recommendation": recommendation,
        "significant_negative_metrics": negatives,
        "primary_metric": "retention_7",
        "guardrail_metrics": ["retention_1", "sum_gamerounds"],
    }


def configure_matplotlib() -> None:
    plt.rcParams.update(
        {
            "font.family": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titleweight": "bold",
            "axes.titlesize": 16,
            "axes.labelsize": 11,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.fontsize": 10,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
        }
    )


def save_figure(fig: plt.Figure, stem: str) -> None:
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg", "pdf"):
        kwargs = {"bbox_inches": "tight", "pad_inches": 0.08}
        if extension == "png":
            kwargs["dpi"] = 300
        fig.savefig(FIGURE_DIR / f"{stem}.{extension}", **kwargs)
    plt.close(fig)


def plot_retention(groups: dict, comparisons: dict) -> None:
    metrics = [
        ("retention_1", "D1 留存"),
        ("retention_7", "D7 留存"),
    ]
    labels = [label for _, label in metrics]
    x = np.arange(len(metrics))
    width = 0.32

    fig, ax = plt.subplots(figsize=(8.2, 5.1))
    for index, group in enumerate(GROUPS):
        values = [groups[group][metric]["rate"] * 100 for metric, _ in metrics]
        lower_errors = []
        upper_errors = []
        for metric, _ in metrics:
            lower, upper = groups[group][metric]["wilson_ci95"]
            rate = groups[group][metric]["rate"]
            lower_errors.append((rate - lower) * 100)
            upper_errors.append((upper - rate) * 100)
        offset = (index - 0.5) * width
        bars = ax.bar(
            x + offset,
            values,
            width,
            yerr=[lower_errors, upper_errors],
            capsize=5,
            color=COLORS[group],
            label=group,
        )
        for bar, value in zip(bars, values):
            ax.annotate(
                f"{value:.1f}%",
                (bar.get_x() + bar.get_width() / 2, value),
                xytext=(0, 8),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=10,
            )

    ax.set_xticks(x, labels)
    ax.set_ylim(0, 55)
    ax.set_ylabel("留存率（%）")
    ax.set_title("两组 D1 / D7 留存率与 95% Wilson 置信区间")
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, ncols=2, loc="upper right")

    d7 = comparisons["retention_7"]
    verdict = (
        "D7 显著下降"
        if d7["p_value"] < 0.05 and d7["difference"] < 0
        else "D7 未见显著下降"
    )
    ax.text(
        0.01,
        0.96,
        verdict,
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=11,
        color="#9A3412",
    )
    fig.tight_layout()
    save_figure(fig, "01_group_retention")


def plot_difference(comparisons: dict) -> None:
    metrics = [
        ("retention_1", "D1 留存"),
        ("retention_7", "D7 留存"),
    ]
    values = [comparisons[key]["difference_pp"] for key, _ in metrics]
    lower_errors = []
    upper_errors = []
    for key, value in zip([key for key, _ in metrics], values):
        lower, upper = comparisons[key]["difference_ci95_pp"]
        lower_errors.append(value - lower)
        upper_errors.append(upper - value)
    errors = [lower_errors, upper_errors]

    fig, ax = plt.subplots(figsize=(8.2, 4.6))
    y = np.arange(len(metrics))
    ax.axvline(0, color="#111827", linewidth=1)
    ax.errorbar(
        values,
        y,
        xerr=errors,
        fmt="o",
        markersize=9,
        capsize=6,
        color="#374151",
        ecolor="#6B7280",
    )
    for y_value, value, (_, label) in zip(y, values, metrics):
        ax.annotate(
            f"{value:+.2f} pp",
            (value, y_value),
            xytext=(8, 0),
            textcoords="offset points",
            va="center",
        )

    ax.set_yticks(y, [label for _, label in metrics])
    ax.set_xlabel("gate_40 - gate_30（百分点）")
    ax.set_title("40 关组相对 30 关组的留存差异")
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()
    save_figure(fig, "02_retention_difference")


def plot_rounds_ecdf(df: pd.DataFrame, rounds: dict) -> None:
    fig, ax = plt.subplots(figsize=(8.2, 5.1))
    clip = rounds["p99_threshold"]
    display = df.assign(
        display_rounds=df["sum_gamerounds"].clip(upper=clip)
    )

    for group in GROUPS:
        values = np.sort(
            display.loc[display["version"] == group, "display_rounds"].to_numpy()
        )
        y = np.arange(1, len(values) + 1) / len(values)
        ax.step(
            values + 1,
            y,
            where="post",
            color=COLORS[group],
            linewidth=2,
            label=group,
        )

    ax.set_xscale("log")
    ax.set_xlabel("累计游戏轮次（对数轴，1 代表 0 轮；图形截尾至 P99）")
    ax.set_ylabel("累计用户占比")
    ax.set_title("两组游戏轮次累计分布接近")
    ax.grid(color="#E5E7EB", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="lower right")
    ax.text(
        0.02,
        0.96,
        f"最大值 {df['sum_gamerounds'].max():,} 轮；统计保留全量数据",
        transform=ax.transAxes,
        va="top",
        fontsize=10,
        color="#4B5563",
    )
    fig.tight_layout()
    save_figure(fig, "03_rounds_distribution")


def plot_bucket_retention(bucket_metrics: list[dict]) -> None:
    frame = pd.DataFrame(bucket_metrics)
    metrics = [
        ("d1_retention", "D1 留存"),
        ("d7_retention", "D7 留存"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.8), sharex=True)
    x = np.arange(len(BUCKET_ORDER))

    for axis, (metric, title) in zip(axes, metrics):
        for group in GROUPS:
            group_frame = (
                frame[frame["version"] == group]
                .set_index("round_bucket")
                .reindex(BUCKET_ORDER)
            )
            axis.plot(
                x,
                group_frame[metric] * 100,
                marker="o",
                linewidth=2,
                markersize=5,
                color=COLORS[group],
                label=group,
            )
        axis.set_title(title)
        axis.set_xticks(x, BUCKET_ORDER, rotation=25)
        axis.set_xlabel("累计游戏轮次分桶")
        axis.set_ylabel("留存率（%）")
        axis.grid(color="#E5E7EB", linewidth=0.8)
        axis.set_axisbelow(True)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncols=2, frameon=False)
    fig.suptitle("留存率随游戏轮次增加而上升", y=0.98, fontsize=16, fontweight="bold")
    fig.text(
        0.5,
        0.01,
        "轮次与留存是描述性关联，不能据此推断因果。",
        ha="center",
        fontsize=9,
        color="#4B5563",
    )
    fig.tight_layout(rect=(0, 0.04, 1, 0.92))
    save_figure(fig, "04_retention_by_rounds")


def build_results(df: pd.DataFrame, sql_results: dict[str, pd.DataFrame], validation: dict) -> dict:
    groups: dict[str, dict] = {}
    for group in GROUPS:
        subset = df[df["version"] == group]
        groups[group] = {
            "users": int(len(subset)),
            "retention_1": {
                "successes": int(subset["retention_1"].sum()),
                "total": int(len(subset)),
                "rate": float(subset["retention_1"].mean()),
                "wilson_ci95": list(
                    wilson_interval(
                        int(subset["retention_1"].sum()), int(len(subset))
                    )
                ),
            },
            "retention_7": {
                "successes": int(subset["retention_7"].sum()),
                "total": int(len(subset)),
                "rate": float(subset["retention_7"].mean()),
                "wilson_ci95": list(
                    wilson_interval(
                        int(subset["retention_7"].sum()), int(len(subset))
                    )
                ),
            },
        }

    comparisons = {}
    for metric in ("retention_1", "retention_7"):
        comparisons[metric] = compare_proportions(
            groups["gate_30"][metric]["successes"],
            groups["gate_30"][metric]["total"],
            groups["gate_40"][metric]["successes"],
            groups["gate_40"][metric]["total"],
        )

    rounds = calculate_round_stats(df)
    bucket_metrics = calculate_bucket_metrics(df)
    decision = decide(groups, comparisons)
    return {
        "dataset": {
            "rows": int(len(df)),
            "groups": GROUPS,
            "source": "DataCamp Mobile Games A/B Testing with Cookie Cats",
            "mirror_url": (
                "https://raw.githubusercontent.com/thegarrickchu/"
                "Mobile-Games-Ab-testing-with-Cookie-Cats/"
                "99580e6e71eb07e39fa113585df5c680e752a064/"
                "datasets/cookie_cats.csv"
            ),
        },
        "validation": validation,
        "groups": groups,
        "comparisons": comparisons,
        "rounds": rounds,
        "bucket_metrics": bucket_metrics,
        "decision": decision,
        "sql_row_counts": {
            name: int(len(frame)) for name, frame in sql_results.items()
        },
    }


def write_outputs(results: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "analysis_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    comparison_rows = []
    for metric, label in (
        ("retention_1", "D1 retention"),
        ("retention_7", "D7 retention"),
    ):
        item = results["comparisons"][metric]
        comparison_rows.append(
            {
                "metric": label,
                "gate_30": results["groups"]["gate_30"][metric]["rate"],
                "gate_40": results["groups"]["gate_40"][metric]["rate"],
                "difference_pp": item["difference_pp"],
                "ci95_low_pp": item["difference_ci95_pp"][0],
                "ci95_high_pp": item["difference_ci95_pp"][1],
                "p_value": item["p_value"],
            }
        )
    pd.DataFrame(comparison_rows).to_csv(
        REPORT_DIR / "results_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )


def run_analysis() -> dict:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    df = load_data()
    validation = validate_data(df)
    db_path = BUILD_DIR / "cookie_cats.db"
    create_database(df, db_path)
    sql_results = run_sql_queries(db_path, SQL_PATH)

    sql_output_dir = BUILD_DIR / "sql_results"
    sql_output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in sql_results.items():
        frame.to_csv(sql_output_dir / f"{name}.csv", index=False)

    results = build_results(df, sql_results, validation)
    write_outputs(results)
    configure_matplotlib()
    plot_retention(results["groups"], results["comparisons"])
    plot_difference(results["comparisons"])
    plot_rounds_ecdf(df, results["rounds"])
    plot_bucket_retention(results["bucket_metrics"])
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cookie Cats gate placement A/B analysis"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the final JSON summary",
    )
    args = parser.parse_args()
    results = run_analysis()
    if not args.quiet:
        print(
            json.dumps(
                {
                    "rows": results["dataset"]["rows"],
                    "recommendation": results["decision"]["recommendation"],
                    "d7_difference_pp": results["comparisons"]["retention_7"][
                        "difference_pp"
                    ],
                    "d7_p_value": results["comparisons"]["retention_7"][
                        "p_value"
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

