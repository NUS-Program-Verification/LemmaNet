# LemmaNet: Agentic Lemma Discovery for Program Verification

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.en.html) [![License: Commercial](https://img.shields.io/badge/License-Commercial-green.svg)](LICENSE) [![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/HfS2zcMzhS)

**Paper**: [ASE 2026](https://arxiv.org/pdf/2603.22114)

---


This repository contains the source code of LemmaNet, an agent prover in Rocq (formerly Coq) 8.18.0.
It is built on top of [AutoRocq](https://github.com/NUS-Program-Verification/AutoRocq), and is designed specifically to prove verification conditions (VCs) from program verification tasks.

The main differentiator of LemmaNet is the ability to discover *helper lemmas*, in two stages:
- [**offline**] Before the actual proving starts, the agent looks at both the annotated source code and the [Frama-C](https://www.frama-c.com/)/WP-produced VC. It does so to encode program structures and semantics *directly* in Rocq. Such encoding is then used to identify bridging lemmas to prove the actual VC.
- [**online**] Once some offline lemmas are prepared, the agent conducts proof search. It is able to propose and prove new helper lemmas as it sees fit. At a high level, it runs in the following loop:

```python
context = get_initial_context()
tools = ['plan', 'tactic', 'context_search', 'rollback', 'helper_lemma']
while not coq.is_proof_complete():
    action = llm.next_action(goal, context)
    coq.apply(action)
    context.update()
    goal.update()
``` 

---

### Directory Structure

```
eval/                              # Directory for eval results
└── final/                         # Final evluation results

benchmark/                         # Directory for benchmark VCs 
├── ntp4vc/                        # NTP4VC (submodule)
├── AutoRocq-bench/                # SV-COMP and more (submodule)
└── *.txt                          # List of VC names used in evaluation

offline-lemma/                     # Directory for offline synthesizer src
└── lemma_discovery.py             # Main script

proof-search/                      # Directory of proof agent src
├── main.py                        # Entry point
├── agent/                         
│   ├── proof_controller.py        # Main loop
│   ├── context_manager.py         # LLM interaction and context management
│   ├── context_search.py          # Local context search
│   ├── history_recorder.py        # Manages proof histories
│   ├── proof_tree.py              # Manages proof tree
│   └── interactive_session.py     # Interactive REPL loop
├── backend/                       # Interface with CoqPyt
├── coqpyt/                        # Interact with Coq
└── utils/                         # Helper functions

scripts/                           # Directory of scripts
├── analyze/                       # Analysis scripts of final results
├── run_discovery.py               # Batch offline lemma generation
├── run.py                         # Batch run
└── get_results.py                 # Parser of .json results
```

### Setup Instructions

1. Install dependencies in Python

```bash
pip install -r requirement.txt
```

2. Install dependencies in opam

```bash
opam switch import deps.opam
```

3. Clone the submodule with

```bash
git submodule update --init --recursive
```

4. Compile `libautorocq` by running

```bash
cd benchmarks/AutoRocq-bench/libautorocq; make
```

5. Configure `library_paths` in `proof-search/configs/default_config.json` to point to `libautorocq`.

### Minimal Example of Proof Agent

1. Set up API key in the config or by running `export OPENAI_API_KEY=...`. If you prefer models from other providers, see [here](proof-search/configs/readme.md).

2. To prove [`examples/example.v`](proof-search/examples/example.v) with a minimal [config](proof-search/configs/minimal.json), go to `proof-search` directory and run:

```bash
python3 -m main examples/example.v --config ./configs/minimal.json
```

If LemmaNet runs successfully, you will be able to see in the terminal
```
[INFO] [Main]: 🎉 Proof completed successfully!
```
and the proof script is saved in the same [`example.v`](proof-search/examples/example.v) file. You will also be able to find saved proof states and aggregated results at `data/`, which can be reused to prove other goals in the future.

For more configurations of the tool, check out the [readme](proof-search/configs/readme.md) or run with `--help` for more options.


### Minimal Example of Lemma Generation

To prove generate offline helper lemmas for [`examples/match_string_assert.v`](proof-search/proof-search/examples/match_string_assert.v), run the following:

```bash
export OPENAI_API_KEY="sk-xxx"
python3 offline-lemma/lemma_discovery.py \
  --source ./proof-search/examples/match_string.c \
  --wp-goal ./proof-search/examples/match_string_assert.v \
  --output-dir ./temp
```

This will take one or two minutes. After completion, you will find the generated files saved in `temp/`:
```bash
- ghost_vc.v                  # An intuitive encoding of the VC
- ghost_vc_helper_lemmas.v    # offline helper lemmas
- proof_plan.txt              # Natural language proof plan
- ghost_vc_log.txt            # Saved log
```

---

### Rerunning Large Experiments

The paper's results can be reproduced by running all two benchmarks: `svcomp-ablation`, `svcomp-remaining`, `ntp4vc-ablation`, `ntp4vc-remaining`. 
The list of VCs included in each benchmark can be found in `benchmarks/*.txt`.
On average, running each theorem with GPT-5.2 costs ~$0.4 for SV-COMP theorems and ~$1.0 for NTP4VC theorems.

#### SV-COMP Benchmark

[AutoRocq-bench](https://github.com/NUS-Program-Verification/AutoRocq-bench) consists of 641 theorems generated by [Frama-C](https://www.frama-c.com/) on [SV-COMP](https://gitlab.com/sosy-lab/sv-comp/bench-defs) programs.

No extra setup is needed.

#### NTP4VC Benchmark

[NTP4VC](https://github.com/xqyww123/NTP4VC) consists of verification conditions generated from various real-world software such as the linux kernel, standard C++ library, Contiki OS, X.509 parser, and more. 

To setup NTP4VC, run the following commands.

1. Build NTP4VC library

```bash
cd benchmarks/ntp4vc/generation/rocq/; dune build
```

2. Go back to root and extract source code

```bash
for archive in benchmarks/ntp4vc/data/why3/frama_c/*/src.tar.zst; do
  name="$(basename "$(dirname "$archive")")"
  dest="benchmarks/ntp4vc/$name"

  mkdir -p "$dest"
  tar --use-compress-program=unzstd -xf "$archive" -C "$dest"
done
```

3. Build and generate configs

```bash
export OPENAI_API_KEY="sk-xxx"
python3 scripts/ntp4vc/build.py
```

This step may take quite long. To change the list of theorems to compile, edit the `main` function of [`scripts/ntp4vc/build.py`](scripts/ntp4vc/build.py). 

#### Batch Run

To batch run large experiments on these benchmarks:

1. Invoke the offline lemma discovery routine to prepare offline lemmas and proof plans:

```bash
export OPENAI_API_KEY="sk-xxx"
python3 scripts/run_discovery.py \
  --benchmark svcomp-ablation \
  --max-items 10
```

2. Run the proof agent with prepared helper lemmas:

```bash
export OPENAI_API_KEY="sk-xxx"
python3 scripts/run.py \
  --benchmark svcomp-ablation \
  --output-dir ./out \
  --max-items 10
```

Here, CLI flag `--max-items` limits the number of items to run in the benchmark (first 10 in this case).

---

### Interactive Mode

<details> 
<summary><b>Details</b></summary>

<br>

In addition to running AutoRocq in a hands-off style, you can *co-develop* Rocq proofs with the agent in interactive mode.
The agent exposes a REPL where you can steer, inspect, and contribute tactics alongside the LLM.

**Starting interactive mode** — pass `--interactive` (or `-i`) on the command line:

```bash
python3 -m main examples/example.v --config ./configs/minimal.json --interactive
```

Or enable it permanently in your config:

```json
{
  "interactive": {
    "enabled": true
  }
}
```

**What interactive mode does**

- **Stepping through proofs** — you can step through AutoRocq's generation and understand its trajectory.
- **Adding hints for agent** — You can add natural language `hint` to guide AutoRocq's proof strategy.
- **Co-writing proofs** — you can directly add `tactic`, print `tree`, run `search`, or `rollback` as you wish. Existing proof steps and manual edits are preserved, AutoRocq picks up exactly where you left.

#### REPL commands

| Command        | Description                                                                                                                                                                                     |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `step`         | Agent takes one action (tactic attempt or rollback), then pauses                                                                                                                                |
| `run`          | Agent runs until the focused goal changes, the agent rolls back, or the proof completes. Failed tactics are handled internally and do not stop `run`                                            |
| `tactic <tac>` | Apply a Rocq tactic directly (bypasses the LLM). Example: `tactic intros n.`                                                                                                                    |
| `hint <text>`  | Inject a natural-language hint into the agent's next prompt. Example: `hint try induction on n`                                                                                                 |
| `rollback [n]` | Undo the last `n` applied tactics (default 1), regardless of whether they were applied by you or the agent. If `n` exceeds the number of applied tactics, rolls back to `Proof.` with a warning |
| `search <cmd>` | Run a Rocq query and print the results (display-only; does not inject into LLM context). Examples: `search Search Z.add`, `search Print Z.add_comm`, `search Check Z.add`                       |
| `status`       | Display the current proof goal and hypotheses                                                                                                                                                   |
| `explain`      | Show agent reasoning history                                                                                                                                                                    |
| `tree`         | Display the current proof tree with tactic history                                                                                                                                              |
| `help`         | Print all available commands                                                                                                                                                                    |
| `quit`         | Exit the session                                                                                                                                                                                |

</details> 

---

### Replicating Results from Paper

<details> 
<summary><b>Reproducing Figures</b></summary>

<br>


- Figure 7 and 8:

```bash
python3 scripts/analyze/draw_results.py \
  ./eval/final/final-svcomp.csv ./eval/final/complexity-svcomp.csv \
  ./eval/final/final-ntp4vc.csv ./eval/final/complexity-ntp4vc.csv
```

- Table 2 and Figure 9:

```bash
python3 scripts/analyze/draw_hl_histogram.py
```

- Table 3:

```bash
python3 scripts/analyze/classify_lemma_names.py
```

</details> 

---

### Citation / Attribution

If you are interested in the work, consider joining the [Discord](https://discord.gg/HfS2zcMzhS) server for the latest discussions/development of agentic program verification!

If you use our work for academic research, please cite our paper:

```
@inproceedings{lemmanet,
  title={Automated Lemma Discovery in Agentic Program Verification},
  author={Zhao, Huan and Tu, Haoxin and Liu, Zhengyao and Rinard, Martin and Roychoudhury, Abhik},
  booktitle={2026 41th IEEE/ACM International Conference on Automated Software Engineering (ASE)},
  year={2026}
}
```
