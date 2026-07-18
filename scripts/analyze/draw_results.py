import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from venn import venn
import re
import os

if len(sys.argv) != 5:
    print(f"Usage: python {sys.argv[0]} <svcomp_results.csv> <svcomp_complexity.csv> <ntp4vc_results.csv> <ntp4vc_complexity.csv>")
    sys.exit(1)


tools = ["lemmanet", "autorocq", "Copra", "hammer_10m", ]
names = ["LemmaNet", "AutoRocq", "Copra", "CoqHammer", ]
colors = [
    "#455db4",
    "#3ba5bf",
    "#ffb968",
    "#ff9999"
]
TEXT_COLOR = "#22304a"
GRID_COLOR = "#d8deea"

plot_size = (9, 6)
plot_ylim = 170

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 13,
    }
)

# Prepare SV-Comp results

svcomp_result_file = sys.argv[1]
complexity_file_1 = sys.argv[2]
svcomp_df = pd.read_csv(svcomp_result_file)
svcomp_complexity = pd.read_csv(complexity_file_1)

# Prepare NTP4VC results

ntp4vc_result_file = sys.argv[3]
complexity_file_2 = sys.argv[4]
ntp4vc_df = pd.read_csv(ntp4vc_result_file)
ntp4vc_complexity = pd.read_csv(complexity_file_2)

def merge_with_complexity(results_df, complexity_df, use_basename=False):
    if not use_basename:
        return pd.merge(results_df, complexity_df, on="lemma_name", how="left")

    results = results_df.copy()
    complexity = complexity_df.copy()
    results["merge_key"] = results["lemma_name"].map(os.path.basename)
    complexity["merge_key"] = complexity["lemma_name"].map(os.path.basename)
    complexity = complexity.drop(columns=["lemma_name"])
    merged = pd.merge(results, complexity, on="merge_key", how="left")
    return merged.drop(columns=["merge_key"])

def normalize_lemma_name(name):
    if pd.isnull(name):
        return ""
    return str(name).split(":")[0].strip().lower()

merged_svcomp = merge_with_complexity(svcomp_df, svcomp_complexity)
merged_ntp4vc = merge_with_complexity(ntp4vc_df, ntp4vc_complexity, use_basename=True)


def get_success_col(df, tool):
    if tool in df.columns:
        return tool
    succ_col = f"{tool}"
    if succ_col in df.columns:
        return succ_col
    return None


def get_success_series(df, tool):
    col = get_success_col(df, tool)
    if col is None:
        return pd.Series(0, index=df.index, dtype=int)
    return pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)


merged_svcomp["lemma_name_norm"] = merged_svcomp["lemma_name"].apply(normalize_lemma_name)
merged_ntp4vc["lemma_name_norm"] = merged_ntp4vc["lemma_name"].apply(normalize_lemma_name)
merged_svcomp["benchmark"] = "SV-COMP"
merged_ntp4vc["benchmark"] = "NTP4VC"
merged_all = pd.concat([merged_svcomp, merged_ntp4vc], ignore_index=True, sort=False)
merged_all["lemma_id"] = merged_all["benchmark"] + "::" + merged_all["lemma_name_norm"]


def style_bar_plot(ax, ylabel_fontsize=20, tick_fontsize=18, legend_fontsize=18):
    ax.set_facecolor("white")
    ax.tick_params(axis="both", labelsize=tick_fontsize, colors=TEXT_COLOR)
    ax.yaxis.label.set_color(TEXT_COLOR)
    ax.xaxis.label.set_color(TEXT_COLOR)
    ax.grid(axis="y", color=GRID_COLOR, linewidth=1.0, alpha=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(GRID_COLOR)
    ax.spines["bottom"].set_color(GRID_COLOR)
    ax.legend(
        frameon=True,
        fontsize=legend_fontsize,
        facecolor="white",
        edgecolor="white",
        framealpha=1.0,
    )

# 1. Total successes per tool on merged SV-COMP + NTP4VC

succ_all = [int(get_success_series(merged_all, tool).sum()) for tool in tools]

tool_success_sets = {}
for tool in tools:
    tool_success_sets[tool] = set(
        merged_all.loc[get_success_series(merged_all, tool) == 1, "lemma_id"]
    )

# 2. Venn diagram for successes on the merged benchmark set
fig, ax = plt.subplots(1, 1, figsize=(7.3, 4.6))
venn(tool_success_sets, cmap=colors, fontsize=15, ax=ax, legend_loc=None)
ax.set_xlim(0.02, 0.98)
ax.set_ylim(0.10, 0.82)
ax.set_position([0.02, 0.06, 0.96, 0.77])


# Single legend at the center top
legend_handles = [mpatches.Patch(color=colors[i], label=names[i]) for i in range(len(tools))]
fig.legend(
    handles=legend_handles,
    loc="upper center",
    ncol=len(tools),
    fontsize=14,
    frameon=False,
    bbox_to_anchor=(0.5, 0.965),
    columnspacing=0.9,
    handletextpad=0.4,
)
plt.savefig("eval/final/success_venn.pdf")


# 3. Breakdown of success by complexity buckets

def draw_success_by_complexity(metric, base, output_name):
    bucket_edges = np.array([0, 1, 2, 3, 10]) * base
    bucket_labels = [f"{bucket_edges[i]:.0f}-{bucket_edges[i+1]-1:.0f}" for i in range(len(bucket_edges)-1)]
    bucket_labels[-1] = f"{bucket_edges[-2]:.0f}+"

    def assign_bucket(val):
        if pd.isnull(val):
            return None
        for i in range(len(bucket_edges)-1):
            if bucket_edges[i] <= val < bucket_edges[i+1]:
                return bucket_labels[i]
        return bucket_labels[-1]

    plot_df = merged_all.copy()
    plot_df["comp_bucket"] = plot_df[metric].apply(assign_bucket)

    bar_data = {tool: [] for tool in tools}
    for label in bucket_labels:
        df_bucket = plot_df[plot_df["comp_bucket"] == label]
        for tool in tools:
            bar_data[tool].append(int(get_success_series(df_bucket, tool).sum()))

    x = np.arange(len(bucket_labels))
    width = 0.15
    fig, ax = plt.subplots(figsize=plot_size)
    fig.patch.set_facecolor("white")
    for idx, tool in enumerate(tools):
        ax.bar(x + idx*width, bar_data[tool], width, color=colors[idx], alpha=1.0, label=names[idx])

    ax.set_xticks(x + width*2)
    ax.set_xticklabels(bucket_labels, fontsize=27)
    ax.set_ylim(0, plot_ylim)
    ax.set_ylabel("# of Proved VCs", fontsize=27, color=TEXT_COLOR)
    style_bar_plot(ax, ylabel_fontsize=27, tick_fontsize=27, legend_fontsize=27)
    plt.tight_layout()
    fig.savefig(output_name, dpi=300, bbox_inches="tight")
    plt.close(fig)


draw_success_by_complexity("term_complexity", 100, "eval/final/success_by_complexity_term.pdf")

draw_success_by_complexity("hypothesis_count", 10, "eval/final/success_by_complexity_hypo.pdf")


# 4. Breakdown of merged successes by lemma type
lemma_types = [
    ("rte", re.compile(r"(rte|valid)", re.IGNORECASE)),
    ("loop", re.compile(r"(loop|invariant|variant)", re.IGNORECASE)),
    ("assertion", re.compile(r"assert", re.IGNORECASE)),
    ("contract", re.compile(r"(post|pre|behavior|disjoint|complete|assign|exit|normal)", re.IGNORECASE)),
]

def get_lemma_type(lemma_name):
    for tname, tpat in lemma_types:
        if tpat.search(lemma_name):
            return tname
    print(f"No lemma type found for {lemma_name}")
    return "other"

type_df = merged_all.copy()
type_df["lemma_type"] = type_df["lemma_name"].astype(str).apply(get_lemma_type)

type_display_map = {
    "rte": "RTE-free",
    "assertion": "Assert",
    "loop": "Loop",
    "contract": "Contract",
}
type_labels = [t[0] for t in lemma_types if (type_df["lemma_type"] == t[0]).any()]
type_labels_display = [type_display_map[t] for t in type_labels]
type_bar_data = {tool: [] for tool in tools}
for tlabel in type_labels:
    df_type = type_df[type_df["lemma_type"] == tlabel]
    print(f"Type {tlabel}: {len(df_type)} lemmas ({len(df_type)/len(type_df)*100:.1f}%)")
    for tool in tools:
        type_bar_data[tool].append(int(get_success_series(df_type, tool).sum()))

x = np.arange(len(type_labels))
width = 0.15
fig, ax = plt.subplots(figsize=plot_size)
fig.patch.set_facecolor("white")
for idx, tool in enumerate(tools):
    bars = ax.bar(x + idx*width, type_bar_data[tool], width, color=colors[idx], alpha=1.0, label=names[idx])
    # for bar in bars:
    #     height = bar.get_height()
    #     plt.text(bar.get_x() + bar.get_width()/2, height, f'{int(height)}', ha='center', va='bottom', fontsize=18)

ax.set_xticks(x + width*2)
ax.set_xticklabels(type_labels_display, fontsize=27)
ax.set_ylim(0, plot_ylim)
ax.set_ylabel("# of Proved VCs", fontsize=27, color=TEXT_COLOR)
style_bar_plot(ax, ylabel_fontsize=27, tick_fontsize=27, legend_fontsize=27)
plt.tight_layout()
fig.savefig("eval/final/success_by_type.pdf", dpi=300, bbox_inches="tight")


