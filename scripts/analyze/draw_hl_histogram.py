#!/usr/bin/env python3

import json
import csv
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NTP4VC_PATH = PROJECT_ROOT / "eval" / "final" / "result-ntp4vc.json"
SVCOMP_PATH = PROJECT_ROOT / "eval" / "final" / "result-svcomp.json"
OUTPUT_PATH = PROJECT_ROOT / "eval" / "final" / "hl-hist.pdf"
SUCCESS_OUTPUT_PATH = PROJECT_ROOT / "eval" / "final" / "hl-success-bars.pdf"
NTP4VC_CSV_PATH = PROJECT_ROOT / "eval" / "final" / "final-ntp4vc.csv"
SVCOMP_CSV_PATH = PROJECT_ROOT / "eval" / "final" / "final-svcomp.csv"
TAIL_BUCKET_START = 22

OFFLINE_COLOR = "#566ec5"
ONLINE_COLOR = "#ffb968"
TEXT_COLOR = "#22304a"
GRID_COLOR = "#d8deea"
ALPHA = 0.8
SUCCESS_FIGURE_WIDTH = 15
SUCCESS_FIGURE_HEIGHT = 6

def load_entries(path: Path) -> list[dict]:
    with path.open() as handle:
        payload = json.load(handle)
    return payload["entries"]


def count_offline_helper_lemmas(helper_lemmas: list[dict]) -> int:
    return sum(
        1
        for lemma in helper_lemmas
        if "Axiom" in lemma.get("proof", "") or "Lemma" in lemma.get("proof", "")
    )


def normalize_csv_lemma_name(lemma_name: str) -> str:
    lemma_name = lemma_name.strip()
    marker = "/rocq/"
    if marker in lemma_name:
        return "rocq/" + lemma_name.split(marker, 1)[1]
    return lemma_name


def load_proof_files() -> tuple[set[str], set[str]]:
    proved: set[str] = set()
    unique: set[str] = set()
    for csv_path in (NTP4VC_CSV_PATH, SVCOMP_CSV_PATH):
        with csv_path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if (row.get("lemmanet") or "").strip() == "1":
                    lemma_name = (row.get("lemma_name") or "").strip()
                    if lemma_name:
                        proof_file = normalize_csv_lemma_name(lemma_name)
                        proved.add(proof_file)
                        copra_result = (row.get("Copra") or "").strip()
                        hammer_result = (row.get("hammer_10m") or "").strip()
                        autorocq_result = (row.get("autorocq") or "").strip()
                        if (
                            copra_result != "1"
                            and hammer_result != "1"
                            and autorocq_result != "1"
                        ):
                            unique.add(proof_file)
    return proved, unique


def collect_counts(entries: list[dict]) -> tuple[list[int], list[int]]:
    counts_by_proof = defaultdict(lambda: {"offline": 0, "online": 0})

    for entry in entries:
        proof_file = entry["proof_file"]
        counts_by_proof[proof_file]["offline"] += count_offline_helper_lemmas(
            entry.get("offline_helper_lemmas", [])
        )
        counts_by_proof[proof_file]["online"] += len(entry.get("online_helper_lemmas", []))

    offline_counts = [counts["offline"] for counts in counts_by_proof.values()]
    online_counts = [counts["online"] for counts in counts_by_proof.values()]
    return offline_counts, online_counts


def collect_used_counts(entries: list[dict], all_proved: set[str], all_unique: set[str]) -> tuple[list[int], list[int]]:
    counts_by_proof = defaultdict(lambda: {"offline": 0, "online": 0})

    for entry in entries:
        proof_file = entry["proof_file"]
        if proof_file not in all_proved:
            continue
        counts_by_proof[proof_file]["offline"] += len(entry.get("used_offline", []))
        counts_by_proof[proof_file]["online"] += len(entry.get("used_online", []))

    max_offline = -1
    max_online = -1
    max_offline_proofs: list[str] = []
    max_online_proofs: list[str] = []
    both_used = 0
    only_offline_used = 0
    only_online_used = 0
    neither_used = 0
    both_used_unique = 0
    only_offline_used_unique = 0
    only_online_used_unique = 0
    neither_used_unique = 0

    for proof_file, counts in counts_by_proof.items():
        offline_used = counts["offline"]
        online_used = counts["online"]

        if offline_used > 0 and online_used > 0:
            both_used += 1
        elif offline_used > 0:
            only_offline_used += 1
        elif online_used > 0:
            only_online_used += 1
        else:
            neither_used += 1

        if proof_file in all_unique:
            if offline_used > 0 and online_used > 0:
                both_used_unique += 1
            elif offline_used > 0:
                only_offline_used_unique += 1
            elif online_used > 0:
                only_online_used_unique += 1
            else:
                neither_used_unique += 1

        if offline_used > max_offline:
            max_offline = offline_used
            max_offline_proofs = [proof_file]
        elif offline_used == max_offline:
            max_offline_proofs.append(proof_file)

        if online_used > max_online:
            max_online = online_used
            max_online_proofs = [proof_file]
        elif online_used == max_online:
            max_online_proofs.append(proof_file)

    print(f"================================================")
    print("Usage coverage by proof file:")
    print(f"  both: {both_used}")
    print(f"  only offline: {only_offline_used}")
    print(f"  only online: {only_online_used}")
    print(f"  neither: {neither_used}")
    print(f"================================================")
    print("Usage coverage by unique proofs:")
    print(f"  both: {both_used_unique}")
    print(f"  only offline: {only_offline_used_unique}")
    print(f"  only online: {only_online_used_unique}")
    print(f"  neither: {neither_used_unique}")

    offline_counts = [counts["offline"] for counts in counts_by_proof.values()]
    online_counts  = [counts["online"] for counts in counts_by_proof.values()]
    return offline_counts, online_counts


def percentages_by_value(counts: list[int], max_count: int) -> np.ndarray:
    if not counts:
        return np.zeros(max_count + 1)
    histogram = np.bincount(counts, minlength=max_count + 1)
    return histogram / len(counts) * 100.0


def clip_counts(counts: list[int], cutoff: int) -> list[int]:
    return [min(count, cutoff) for count in counts]


def smooth_series(values: np.ndarray, sigma: float = 1.2) -> np.ndarray:
    radius = max(1, int(3 * sigma))
    grid = np.arange(-radius, radius + 1)
    kernel = np.exp(-(grid ** 2) / (2 * sigma ** 2))
    kernel /= kernel.sum()
    return np.convolve(values, kernel, mode="same")


def plot_successful_discovered_used(
    entries: list[dict],
    successful_proofs: set[str],
    proofs: set[str],
) -> None:
    counts_by_proof = defaultdict(
        lambda: {"disc_offline": 0, "disc_online": 0, "used_offline": 0, "used_online": 0}
    )

    for entry in entries:
        proof_file = entry["proof_file"]
        if proof_file not in successful_proofs or proof_file not in proofs:
            continue
        counts_by_proof[proof_file]["disc_offline"] += count_offline_helper_lemmas(entry["offline_helper_lemmas"])
        counts_by_proof[proof_file]["disc_online"]  += len(entry["online_helper_lemmas"])
        counts_by_proof[proof_file]["used_offline"] += len(entry["used_offline"])
        counts_by_proof[proof_file]["used_online"]  += len(entry["used_online"])

    ranked = sorted(
        (
            item
            for item in counts_by_proof.items()
            if (item[1]["used_offline"] + item[1]["used_online"]) > 0
        ),
        key=lambda item: (
            item[1]["disc_offline"] + item[1]["disc_online"],
            item[1]["used_offline"] + item[1]["used_online"],
        ),
        reverse=True,
    )
    if not ranked:
        return

    x_values = np.arange(len(ranked))
    discovered_offline = np.array([item[1]["disc_offline"] for item in ranked], dtype=float)
    discovered_online = np.array([item[1]["disc_online"] for item in ranked], dtype=float)
    used_offline = np.array([item[1]["used_offline"] for item in ranked], dtype=float)
    used_online = np.array([item[1]["used_online"] for item in ranked], dtype=float)

    fig, ax = plt.subplots(figsize=(SUCCESS_FIGURE_WIDTH, SUCCESS_FIGURE_HEIGHT))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    bar_width = 1.0
    ax.bar(
        x_values,
        discovered_online,
        width=bar_width,
        color=ONLINE_COLOR,
        alpha=ALPHA,
        edgecolor="white",
        linewidth=0.3,
        label="Online lemmas discovered",
        zorder=2,
    )
    ax.bar(
        x_values,
        discovered_offline,
        width=bar_width,
        bottom=discovered_online,
        color=OFFLINE_COLOR,
        alpha=ALPHA,
        edgecolor="white",
        linewidth=0.3,
        label="Offline lemmas discovered",
        zorder=2,
    )

    ax.bar(
        x_values,
        used_online,
        width=bar_width,
        color="none",
        edgecolor="black",
        hatch="///",
        linewidth=0.35,
        label="Online lemmas used",
        zorder=3,
    )
    ax.bar(
        x_values,
        used_offline,
        width=bar_width,
        bottom=discovered_online,
        color="none",
        edgecolor="black",
        hatch="///",
        linewidth=0.35,
        label="Offline lemmas used",
        zorder=3,
    )

    ax.set_ylabel("# of helper lemmas", fontsize=25, color=TEXT_COLOR)
    ax.set_xlim(-0.5, len(ranked) - 0.5)
    ax.tick_params(axis="y", labelsize=18, colors=TEXT_COLOR)
    ax.set_xticks([])
    ax.set_xticklabels([])

    ax.grid(axis="y", color=GRID_COLOR, linewidth=1.0, alpha=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(GRID_COLOR)
    ax.spines["bottom"].set_color(GRID_COLOR)

    legend_handles = [
        Patch(facecolor=OFFLINE_COLOR, edgecolor="white", alpha=ALPHA, label="Offline Lemmas Discovered"),
        Patch(facecolor=ONLINE_COLOR,  edgecolor="white", alpha=ALPHA, label="Online Lemmas Discovered"),
        Patch(facecolor="white", edgecolor="black", hatch="///", label="Lemmas Used in Proof"),
    ]
    ax.legend(
        handles=legend_handles,
        frameon=True,
        fontsize=25,
        facecolor="white",
        edgecolor="white",
        framealpha=1.0,
        ncol=1,
    )

    SUCCESS_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    fig.savefig(SUCCESS_OUTPUT_PATH, dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    if not NTP4VC_PATH.exists():
        raise FileNotFoundError(f"Input file not found: {NTP4VC_PATH}")
    if not SVCOMP_PATH.exists():
        raise FileNotFoundError(f"Input file not found: {SVCOMP_PATH}")
    if not NTP4VC_CSV_PATH.exists():
        raise FileNotFoundError(f"Input file not found: {NTP4VC_CSV_PATH}")
    if not SVCOMP_CSV_PATH.exists():
        raise FileNotFoundError(f"Input file not found: {SVCOMP_CSV_PATH}")
    
    entries = load_entries(NTP4VC_PATH)
    entries.extend(load_entries(SVCOMP_PATH))
    offline_counts, online_counts = collect_counts(entries)
    proved_proof_files, unique_proof_files = load_proof_files()

    offline_counts = clip_counts(offline_counts, TAIL_BUCKET_START)
    online_counts = clip_counts(online_counts, TAIL_BUCKET_START)

    max_count = max(offline_counts + online_counts, default=0)
    x_values = np.arange(max_count + 1)
    offline_percentages = percentages_by_value(offline_counts, max_count)
    online_percentages = percentages_by_value(online_counts, max_count)
    offline_smooth = smooth_series(offline_percentages)
    online_smooth = smooth_series(online_percentages)

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 13,
        }
    )

    fig, ax = plt.subplots(figsize=(9, 4))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    bar_width = 0.42

    ax.bar(
        x_values + bar_width / 2,
        offline_percentages,
        width=bar_width,
        alpha=ALPHA,
        color=OFFLINE_COLOR,
        edgecolor="white",
        linewidth=1.0,
        label="Offline Helper Lemmas",
        zorder=2,
    )
    ax.bar(
        x_values - bar_width / 2,
        online_percentages,
        width=bar_width,
        alpha=ALPHA,
        color=ONLINE_COLOR,
        edgecolor="white",
        linewidth=1.0,
        label="Online Helper Lemmas",
        zorder=2,
    )
    ax.plot(
        x_values + bar_width / 2,
        offline_smooth,
        color=OFFLINE_COLOR,
        linewidth=2.5,
        zorder=3,
    )
    ax.plot(
        x_values - bar_width / 2,
        online_smooth,
        color=ONLINE_COLOR,
        linewidth=2.5,
        zorder=3,
    )

    ax.set_xlabel("# of Helper Lemmas Discovered", fontsize=15, color=TEXT_COLOR)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:.0f}%"))
    xticks = np.arange(0, max_count + 1, max(1, (max_count + 11) // 12))
    if max_count not in xticks:
        xticks = np.append(xticks, max_count)
    xtick_labels = [str(tick) for tick in xticks]
    if max_count == TAIL_BUCKET_START:
        xtick_labels[-1] = f"{TAIL_BUCKET_START}+"
    ax.set_xticks(xticks)
    ax.set_xticklabels(xtick_labels)
    ax.tick_params(axis="both", labelsize=13, colors=TEXT_COLOR)
    ax.grid(axis="y", color=GRID_COLOR, linewidth=1.0, alpha=0.8)
    ax.set_axisbelow(True)

    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(GRID_COLOR)
    ax.spines["bottom"].set_color(GRID_COLOR)

    ax.legend(
        frameon=True,
        fontsize=15,
        facecolor="white",
        edgecolor="white",
        framealpha=1.0,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    fig.savefig(OUTPUT_PATH, dpi=300, bbox_inches="tight")
    plt.close(fig)
    plot_successful_discovered_used(entries, proved_proof_files, proved_proof_files)

    print(f"Saved figure to {OUTPUT_PATH}")
    print(f"Saved figure to {SUCCESS_OUTPUT_PATH}")
    print("Statistics:")
    print(f"================================================")
    print(f"Total proof files: {len(offline_counts)}")
    print(f"Offline discovery mean: {np.mean(offline_counts):.2f}")
    print(f"Online discovery mean: {np.mean(online_counts):.2f}")
    print(f"================================================")
    print(f"Successful proofs = {len(proved_proof_files)}")
    print(f"Unique proofs = {len(unique_proof_files)}")
    collect_used_counts(entries, proved_proof_files, unique_proof_files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
