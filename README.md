# LemmaNet: Agentic Lemma Discovery for Program Verification

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.en.html) [![License: Commercial](https://img.shields.io/badge/License-Commercial-green.svg)](LICENSE) [![Discord](https://img.shields.io/badge/Discord-Join%20Server-5865F2?logo=discord&logoColor=white)](https://discord.gg/HfS2zcMzhS)

**Paper**: [ASE 2026](https://arxiv.org/pdf/2603.22114)

**Quick Links**: [Start with Docker](#quickstart-docker) | [How lemma discovery works](#how-lemma-discovery-works) | [Interactive mode](#interactive-mode)

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

This allows LemmaNet to prove more VCs faster:

| Benchmark | CoqHammer | AutoRocq | **LemmaNet** |
| --- | --- | --- | --- |
| [SV-COMP](https://github.com/NUS-Program-Verification/AutoRocq-bench) | 13.3% | 38.5% | **46.5%** |
| [NTP4VC](https://github.com/xqyww123/NTP4VC)  | 12.7% | 13.3% | **22.0%** |

Reported numbers are percentage of VCs proved with GPT-5.2. 

NTP4VC VCs come from real-world code — the Linux kernel, the C++ stdlib, Contiki OS, an X.509 parser, etc.

Per-goal results and ablations are in [`eval/final/`](eval/final/); 
see the [paper](https://arxiv.org/pdf/2603.22114) for the full setup.


---

### How Lemma Discovery Works

Take `match_string` from the Linux kernel ([`examples/match_string.c`](proof-search/examples/match_string.c)), annotated with a loop invariant and an assertion:

```c
/*@ loop invariant \forall size_t k;
       0 <= k < index ==> valid_str(array[k]);
 */
for (index = 0; index < n; index++) {
    item = array[index];
    if (!item) break;
    //@ assert valid_str(array[index]);
```

Frama-C/WP discharges this into a Rocq VC where `valid_str` has become an opaque predicate `P_valid_str` over WP's memory model, the array access has become `t2 (shift a i)`, and the invariant is quantified over `0 <= k < i + 1`. 
The connection to the assertion is obvious to a human and invisible to `lia` or a hammer.

The offline stage reads the C source *and* the VC together, and synthesizes the bridge ([`examples/match_string_assert.v`](proof-search/examples/match_string_assert.v)):

```coq
Lemma assert_P_valid_str_at_index :
  forall (t : Z -> Z) (t1 : addr -> Z) (t2 : addr -> addr) (a : addr) (i : Z),
    0 <= i ->
    (forall k : Z, 0 <= k < i + 1 -> P_valid_str t t1 (t2 (shift a k))) ->
    P_valid_str t t1 (t2 (shift a i)).
Proof.
  intros t t1 t2 a i Hi Hinv.
  apply Hinv; lia.
Qed.
```

The lemma is trivial once stated — the difficulty is *stating* it, which requires knowing that the assertion at `index` is the loop invariant instantiated at `k = index`. 
That is the information recoverable from the source but erased by the VC generator. 
The online stage then proposes further lemmas during search, and proved lemmas are cached and replayed across goals.

To reproduce this example yourself, follow the [quickstart guide](#quickstart-docker) and run the offline synthesizer on the same pair of files:

```bash
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

## Getting Started

Either path below leaves you able to run [your first proof](#proving-your-first-goal). 
Every command that calls an LLM needs an API key — set it in the config or with `export OPENAI_API_KEY=...`. 
For models from other providers, see the [config readme](proof-search/configs/readme.md).

### Quickstart (Docker)

The image pins Rocq 8.18.0 and the full `opam switch`, so you do not need a local OCaml/Rocq toolchain:

```bash
docker build -t lemmanet -f dockerfile/agent.dockerfile .
```

```bash
docker run -it --rm -e OPENAI_API_KEY="sk-xxx" lemmanet
```

You land in `/LemmaNet/proof-search` with `libautorocq` already built and `configs/default_config.json` already pointing at it, so you can skip straight to proving. 
To work on your own files, mount them with `-v "$PWD:/work"`.

### Local Installation

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

### Proving Your First Goal

From the `proof-search` directory, prove [`examples/example.v`](proof-search/examples/example.v) with the minimal [config](proof-search/configs/minimal.json):

```bash
python3 -m main examples/example.v --config ./configs/minimal.json
```

If LemmaNet runs successfully, you will be able to see in the terminal
```
[INFO] [Main]: 🎉 Proof completed successfully!
```
and the proof script is saved in the same [`example.v`](proof-search/examples/example.v) file. You will also be able to find saved proof states and aggregated results at `data/`, which can be reused to prove other goals in the future.

For more configurations of the tool, check out the [readme](proof-search/configs/readme.md) or run with `--help` for more options.

---

### Interactive Mode



In addition to running LemmaNet in a hands-off style, you can *co-develop* Rocq proofs with the agent in interactive mode.
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

- **Stepping through proofs** — you can step through LemmaNet's generation and understand its trajectory.
- **Adding hints for agent** — You can add natural language `hint` to guide LemmaNet's proof strategy.
- **Co-writing proofs** — you can directly add `tactic`, print `tree`, run `search`, or `rollback` as you wish. Existing proof steps and manual edits are preserved, LemmaNet picks up exactly where you left.
- **Proposing helper lemmas** — you can introduce your own helper lemma with `lemma`, and `drop` a sub-proof that is not working out. User-proposed and agent-proposed lemmas go through the same path, so yours are cached and replayed just the same.

#### REPL commands

| Command        | Description                                                                                                                                                                                     |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `step`         | Agent takes one action (tactic attempt or rollback), then pauses                                                                                                                                |
| `run`          | Agent runs until the focused goal changes, the agent rolls back, or the proof completes. Failed tactics are handled internally and do not stop `run`                                            |
| `tactic <tac>` | Apply a Rocq tactic directly (bypasses the LLM). Example: `tactic intros n.`                                                                                                                    |
| `lemma <stmt>` | Introduce a helper lemma and enter its sub-proof. Name it explicitly (`lemma Hpos: 0 <= n`) or let one be generated (`lemma 0 <= n`). If the same lemma was proved before, its cached proof is replayed and the sub-proof closes immediately. Disabled when `ablation.enable_helper_lemma` is `false` |
| `drop`         | Abandon the current helper lemma sub-proof, removing its `assert` and every tactic tried inside it, and return to the parent goal untouched                                                     |
| `admit`        | Admit the current helper lemma sub-proof to move on. The admitted lemma is *not* recorded, and the enclosing proof can no longer be closed with `Qed` until it is dropped or rolled back        |
| `hint <text>`  | Inject a natural-language hint into the agent's next prompt. Example: `hint try induction on n`                                                                                                 |
| `rollback [n]` | Undo the last `n` applied tactics (default 1), regardless of whether they were applied by you or the agent. A rollback landing on a helper lemma's `{` removes its `assert` too. If `n` exceeds the number of applied tactics, rolls back to `Proof.` with a warning |
| `search <cmd>` | Run a Rocq query and print the results (display-only; does not inject into LLM context). Examples: `search Search Z.add`, `search Print Z.add_comm`, `search Check Z.add`                       |
| `status`       | Display the current proof goal and hypotheses. Inside a helper lemma, the sub-proof being proved is shown above the goal                                                                        |
| `explain`      | Show agent reasoning history, including the last helper lemma proposed and the agent's stated purpose for it                                                                                    |
| `tree`         | Display the current proof tree with tactic history; helper lemma sub-proofs are marked                                                                                                          |
| `help`         | Print all available commands                                                                                                                                                                    |
| `quit`         | Exit the session                                                                                                                                                                                |

While a helper lemma sub-proof is open, the prompt shows which lemma you are proving — `lemmanet[Hpos]>`, or `lemmanet[Hinner @2]>` when nested.

---

### Reproducing the Paper

<details> 
<summary><b>Reproducing Key Experiments</b></summary>

<br>

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
python3 scripts/ntp4vc/build.py
```

This step may take quite long. To change the list of theorems to compile, edit the `main` function of [`scripts/ntp4vc/build.py`](scripts/ntp4vc/build.py). 

#### Batch Run

To batch run large experiments on these benchmarks:

1. Invoke the offline lemma discovery routine to prepare offline lemmas and proof plans:

```bash
python3 scripts/run_discovery.py \
  --benchmark svcomp-ablation \
  --max-items 10
```

2. Run the proof agent with prepared helper lemmas:

```bash
python3 scripts/run.py \
  --benchmark svcomp-ablation \
  --output-dir ./out \
  --max-items 10
```

Here, CLI flag `--max-items` limits the number of items to run in the benchmark (first 10 in this case).

</details> 

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
