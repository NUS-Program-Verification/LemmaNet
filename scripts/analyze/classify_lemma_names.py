#!/usr/bin/env python3

import re
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = PROJECT_ROOT / "eval" / "final" / "lemma_names.txt"

CATEGORY_RULES = [
    {
        "name": "string",
        "keywords": [
            "str",
            "strlen",
            "strnlen",
            "strchrnul",
            "sysfs",
            "ascii",
            "digit",
            "byte",
            "bytes",
            "char",
            "tolower",
            "toupper",
        ],
    },
    {
        "name": "data_structure",
        "keywords": [
            "linked",
            "arr",
            "array",
            "list",
            "segment",
            "slice",
            "memb",
            "multiset",
            "partition",
            "sorted",
            "heap",
            "count_occ",
            "firstn",
            "nth",
            "swipe",
            "map",
            "set",
            "matrix",
            "arr",
        ],
    },
    {
        "name": "memory_address",
        "keywords": [
            "addr",
            "offset",
            "shift",
            "base",
            "region",
            "null",
            "aoff",
            "ptr",
            "mk_addr",
            "valid",
            "separated",
            "included",
            "subrange",
            "interval",
            "range",
            "bound",
            "bounds",
            "unchanged",
            "havoc",
            "read",
            "load",
            "store",
            "updt",
            "update",
            "upd",
            "mem",
            "framed",
        ],
    },
    {
        "name": "simplification",
        "keywords": [
            "iff",
            "simpl",
            "rewrite",
            "normalize",
            "conj",
            "disj",
            "morphism",
            "sym",
            "bridge",
            "chain",
            "elim",
            "disjoint",
            "implies",
            "shape",
            "reduce",
            "reduces",
            "reduction",
            "independent",
            "template",
            "weaken",
            "step",
            "split",
            "case",
            "use",
            "unfold",
            "core",
            "mirror",
            "pattern",
            "from",
            "fixed",
            "repaired",
            "form",
            "instantiation",
            "instantiate",
            "apply",
            "sep",
            "separation",
            "pack",
            "repack",
            "resolve",
            "drop",
            "replace",
            "rename",
            "main",
            "wp",
            "goal",
        ],
    },
    {
        "name": "typing",
        "keywords": [
            "type",
            "uint",
            "sint",
            "int",
            "float",
            "quat",
            "rmat",
            "vect",
            "bool",
            "u64",
            "uint32",
            "uint64",
            "uint16",
            "uint8",
            "sint32",
            "sint8",
            "int32",
            "int64",
            "int16",
            "int8",
            "int32",
            "int8",
            "int32max",
            "real",
            "z",
            "INT",
            "MAX",
            "2147483646",
            "2147483645",
            "2147483647",
            "715827882",
            "2AAAAAAA",
            "3fffffff",
            "7fffffff",
        ],
    },
    {
        "name": "arithmetic",
        "keywords": [
            "lt",
            "le",
            "ge",
            "gt",
            "eq",
            "neq",
            "quot",
            "rem",
            "add",
            "sub",
            "mul",
            "div",
            "mod",
            "pred",
            "succ",
            "plus",
            "minus",
            "square",
            "sign",
            "nonneg",
            "neg",
            "pos",
            "rem",
            "sum",
            "bit",
            "bits",
            "lsr",
            "land",
            "lor",
            "lxor",
            "lnot",
            "arith",
            "odd",
            "even",
            "value",
            "pow2",
            "100000",
            "10000",
            "99999",
            "9999",
            "99",
        ],
    },
]


def tokenize(name: str) -> set[str]:
    return {token for token in re.split(r"[_']+", name.lower()) if token}


def matched_keywords(name: str, category_keywords: list[str]) -> list[str]:
    if name.startswith("H"):
        name = name[1:]
    normalized = name.lower()
    tokens = tokenize(name)
    matches = []
    for keyword in category_keywords:
        if keyword in normalized or keyword in tokens:
            matches.append(keyword)
    return matches


def classify_name(name: str) -> tuple[str, list[str]]:
    for rule in CATEGORY_RULES:
        matches = matched_keywords(name, rule["keywords"])
        if matches:
            return rule["name"], matches
    return "unknown", []


def main() -> int:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_PATH}")

    names = [line.strip() for line in INPUT_PATH.read_text().splitlines() if line.strip()]
    counts = Counter()

    for name in names:
        category, matches = classify_name(name)
        counts[category] += 1

    print(f"Total lemma names: {len(names)}")
    print()
    for category, count in counts.most_common():
        print(f"{category}: {count} ({100.0 * count / len(names):.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
