"""
InteractiveSessionManager: REPL wrapper around ProofController for collaborative
human-agent proof development.

Commands:
  step              — agent takes one tactic/rollback action
  run               — agent runs until subgoal changes, failure, or proof complete
  tactic <tac>      — apply user-supplied tactic
  lemma <stmt>      — introduce a helper lemma and enter its sub-proof
  drop              — abandon the current helper lemma sub-proof
  admit             — admit the current helper lemma sub-proof and move on
  hint <text>       — inject natural-language hint into next agent step
  rollback [n]      — undo last n tactics (user or agent, default 1)
  search <cmd>      — run a Rocq query (e.g. Search Z.add, Print Z.add_comm, Check Z.add)
  status            — display current proof state
  tree              — display proof tree
  explain           — show agent reasoning trace
  help              — show this help
  quit              — exit
"""

import re
from pathlib import Path
from typing import Optional, Tuple

from agent.proof_controller import ProofController
from agent import visualizer
from utils.coq_utils import format_assert_statement
from utils.logger import setup_logger

_COMMANDS = [
    "step", "run", "tactic ", "lemma ", "drop", "admit",
    "hint ", "rollback", "search ",
    "status", "tree", "explain", "help", "quit",
]

# "Hfoo: 0 <= n" names the lemma; "forall x : Z, ..." does not, so it stays bare.
_NAMED_LEMMA_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_']*)\s*:\s*(.+)$", re.DOTALL)


def _split_lemma_arg(arg: str) -> Tuple[str, str]:
    """Split a `lemma` argument into (name, statement); name is '' if omitted."""
    match = _NAMED_LEMMA_RE.match(arg.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", arg.strip()


class InteractiveSessionManager:
    def __init__(self, controller: ProofController):
        self.controller: ProofController = controller
        self._gen = None
        self._done: bool = False
        self.logger = setup_logger("InteractiveSession")
        self._readline_available = False
        self._history_file: Optional[Path] = None
        self._setup_readline()

    def _setup_readline(self):
        try:
            import readline
        except ImportError:
            return # not available on Windows

        self._readline_available = True
        self._history_file = Path.home() / ".autorocq_history"

        if self._history_file.exists():
            try:
                readline.read_history_file(str(self._history_file))
            except OSError:
                pass

        readline.set_completer(self._completer)
        readline.parse_and_bind("tab: complete")

    def _completer(self, text: str, state: int) -> Optional[str]:
        matches = [c for c in _COMMANDS if c.startswith(text)]
        return matches[state] if state < len(matches) else None

    def _save_readline_history(self):
        if not self._readline_available or self._history_file is None:
            return
        try:
            import readline
            readline.write_history_file(str(self._history_file))
        except ImportError:
            pass # not available on Windows
        except OSError:
            pass

    #####################
    ## Public API      ##
    #####################

    def start(self, theorem_name: Optional[str] = None):
        """Initialize the proof session and run the REPL."""
        self.logger.debug(f"Starting interactive session: theorem={theorem_name!r}")

        preexisting = self._extract_and_reset_tactics()

        if not self.controller._init_proof_session(theorem_name):
            print("❌ Failed to initialize proof session.")
            return False

        self._gen = self.controller.step_generator()
        self._done = False

        if preexisting:
            print(f"🔄 Replaying {len(preexisting)} pre-existing tactic(s)...")
            self.logger.debug(f"Pre-existing tactics to replay: {preexisting}")
            for tactic in preexisting:
                self._do_user_tactic(tactic, silent=True)
                if self._done:
                    break
            # Replayed braces may leave us inside a sub-proof; resync the stack.
            self.controller._refresh_helper_lemma_stack_from_history()

        self._display_state()
        if not self._done:
            print("Interactive mode. Type 'help' for available commands.")
            self._do_help()
            self._repl()

        self.controller._finish_proof(self.controller._tactics_with_states)
        return self.controller.is_successful

    #####################
    ## REPL            ##
    #####################

    def _prompt(self) -> str:
        """Prompt showing which helper lemma sub-proof, if any, is being proved."""
        open_lemmas = self.controller.helper_lemma_context()
        if not open_lemmas:
            return "lemmanet> "
        name = open_lemmas[-1]['name'] or 'lemma'
        depth = len(open_lemmas)
        return f"lemmanet[{name}]> " if depth == 1 else f"lemmanet[{name} @{depth}]> "

    def _repl(self):
        while not self._done:
            try:
                raw = input(self._prompt()).strip()
            except (EOFError, KeyboardInterrupt) as e:
                self.logger.debug(f"REPL interrupted: {e}")
                self._do_quit()
                return

            if not raw:
                continue

            cmd, _, arg = raw.partition(" ")
            self.logger.debug(f"User Command: {raw!r}")
            match cmd:
                case "step":
                    self._do_step()
                case "run":
                    self._do_run()
                case "tactic":
                    self._do_user_tactic(arg.strip())
                case "lemma":
                    self._do_lemma(arg.strip())
                case "drop":
                    self._do_drop()
                case "admit":
                    self._do_admit()
                case "hint":
                    self._do_hint(arg.strip())
                case "rollback":
                    n = 1
                    if arg.strip():
                        try:
                            n = int(arg.strip())
                            if n <= 0:
                                raise ValueError
                        except ValueError:
                            print("Usage: rollback [n]  (n must be a positive integer)")
                            continue
                    self._do_rollback(n)
                case "search":
                    self._do_search(arg.strip())
                case "status":
                    self._display_state()
                case "tree":
                    self._do_tree()
                case "explain":
                    self._do_explain()
                case "help":
                    self._do_help()
                case "quit":
                    self._do_quit()
                    return
                case _:
                    print(f"Unknown command: {cmd!r}")
                    self._do_help()

    #####################
    ## Commands        ##
    #####################

    def _do_step(self):
        result = self._advance_one()
        if result is None:
            return
        self._report(result)
        if result['type'] != 'done':
            self._display_state()

    def _do_run(self):
        goals_before = self.controller.coq.get_goal_str()
        while not self._done:
            result = self._advance_one()
            if result is None:
                return
            self._report(result)
            if result['type'] == 'done':
                return
            if result['type'] == 'rollback':
                self._display_state()
                return
            if result.get('proof_complete'):
                return
            current_goals = self.controller.coq.get_goal_str()
            if current_goals != goals_before:
                self._display_state()
                return

    def _do_user_tactic(self, tactic: str, silent: bool = False, record_helper_lemma: bool = True):
        if not tactic:
            print("Usage: tactic <tactic_string>")
            return

        # Braces are steps in their own right and take no period.
        if not tactic.endswith('.') and tactic not in ('{', '}'):
            tactic += '.'

        subgoals_before = self.controller.coq.get_subgoals()
        goals_before = self.controller.coq.get_goal_str()
        hyps_before = self.controller.coq.get_hypothesis()

        success = self.controller.coq.apply_tactic(tactic)
        if not success:
            error = self.controller.coq.get_last_error()
            self.logger.debug(f"User Tactic failed: {tactic!r} — {error}")
            print(f"Tactic failed: {error}")
            return

        goals_after = self.controller.coq.get_goal_str()
        hyps_after = self.controller.coq.get_hypothesis()
        subgoals_after = self.controller.coq.get_subgoals()

        self.controller.global_step_id += 1
        tactic_with_state = self.controller._handle_successful_tactic(
            tactic, subgoals_before, subgoals_after,
            goals_before or '', goals_after or '',
            hyps_before or '', hyps_after or ''
        )
        tactic_with_state['source'] = 'user'
        self.controller._tactics_with_states.append(tactic_with_state)
        self.logger.debug(f"User Tactic applied: {tactic!r}")

        if not silent:
            print(f"✅ Tactic applied: {tactic}")

        status = self.controller.coq.get_proof_completion_status()
        if status.get('is_complete') and status.get('qed_already_applied'):
            print("🎉 Proof complete!")
            self.controller.is_successful = True
            self._done = True
            return

        closed = self.controller.close_helper_lemma_if_complete(
            subgoals_after, goals_after or '', hyps_after or '',
            source='user', record=record_helper_lemma,
        )
        if not silent:
            if closed is not None:
                print(visualizer.render_action('helper_lemma_closed', closed, True), end='')
            self._display_state()

    def _do_lemma(self, arg: str):
        if not self.controller.helper_lemma_enabled():
            print("Helper lemmas are disabled for this session (ablation.enable_helper_lemma = false).")
            return
        if not arg:
            print("Usage: lemma [<name>:] <statement>   e.g. lemma Hpos: 0 <= n")
            return

        name, statement = _split_lemma_arg(arg)
        assert_statement = format_assert_statement(statement, name)
        self.logger.debug(f"User helper lemma: {assert_statement!r}")
        self.controller.global_step_id += 1
        result = self.controller.open_helper_lemma(assert_statement, source='user')

        # open_helper_lemma renders the failure itself
        if not result['success']:
            return

        if result['replayed']:
            print("↺ Proved automatically from a cached proof — back in the parent proof.")
        else:
            print(
                f"Now proving the helper lemma "
                f"(depth {result['depth']}/{self.controller.MAX_HELPER_LEMMA_DEPTH}). "
                "Use 'drop' to abandon it."
            )
        self._display_state()

    def _do_drop(self):
        result = self.controller.abandon_helper_lemma()
        if not result['success']:
            print(result['message'])
            return
        label = result['name'] or result['assert_statement']
        print(f"Dropped helper lemma {label} ({result['rollback_distance']} step(s) removed).")
        self._display_state()

    def _do_admit(self):
        if not self.controller.helper_lemma_context():
            print("admit only applies inside a helper lemma sub-proof; use 'rollback' or 'quit' otherwise.")
            return
        print("⚠️  Admitting this sub-proof — the enclosing proof can no longer be closed with Qed.")
        print("    Use 'drop' or 'rollback' to remove it before finishing the proof.")
        self._do_user_tactic("admit.", record_helper_lemma=False)

    def _do_hint(self, hint: str):
        if not hint:
            print("Usage: hint <text>")
            return
        self.logger.debug(f"User Hint queued: {hint!r}")
        self.controller._pending_hints.append(hint)
        print("Hint queued for next agent step.")

    def _do_rollback(self, n: int):
        history = self.controller._tactics_with_states
        if not history:
            print("No tactics to roll back.")
            return

        actual_n = min(n, len(history))
        if actual_n < n:
            print(f"Warning: only {len(history)} tactic(s) applied; rolling back all.")

        # Via the controller, so landing on a lemma's '{' also drops its assert.
        proof_tree_str = (
            self.controller.proof_tree.get_proof_tree_string()
            if self.controller.proof_tree is not None else ''
        )
        result = self.controller._execute_rollback(
            history, reason='User rollback', proof_tree_str=proof_tree_str, rb_steps=actual_n
        )
        if not result['success']:
            print(visualizer.render_action(
                'rollback', {'reason': 'User rollback', 'message': result['message']}, False
            ), end='')
            return

        target_step_number = result['target_step_number']
        if result['target_index'] > 0:
            self.controller._tactics_with_states[:] = [
                t for t in self.controller._tactics_with_states
                if t['step_number'] <= target_step_number
            ]
        else:
            self.controller._tactics_with_states[:] = []
        self.controller._refresh_helper_lemma_stack_from_history()

        self.logger.debug(f"User rollback: {result['rollback_distance']} tactic(s)")
        print(f"Rolled back {result['rollback_distance']} tactic(s).")
        self._display_state()

    def _do_search(self, cmd: str):
        if not cmd:
            print("Usage: search <Rocq command>  (e.g. search Search Z.add  |  search Print Z.add_comm)")
            return
        cs = self.controller.context_manager.context_search
        if cs is None:
            print("Context search is disabled in this session.")
            return
        goal_context = self.controller.coq.get_goal_str() or ""
        result = cs.search(cmd, goal_context=goal_context)
        if result and result.content:
            print(result.content)
        else:
            print(f"No results for '{cmd}'.")

    def _do_explain(self):
        print(visualizer.render_explain(self.controller.context_manager))

    def _do_help(self):
        print("Commands:")
        print("  step              — agent takes one action (tactic or rollback)")
        print("  run               — agent runs until subgoal changes, rollback, or proof complete")
        print("  tactic <tac>      — apply a user-supplied tactic")
        print("  hint <text>       — inject hint into next agent step")
        print("  rollback [n]      — undo last n tactics, user or agent (default 1)")
        print("  search <cmd>      — run a Rocq query, e.g. 'search Search Z.add' or 'search Print Z.add_comm'")
        print("  lemma <stmt>      — introduce a helper lemma, e.g. 'lemma Hpos: 0 <= n'")
        print("  drop              — abandon the current helper lemma sub-proof")
        print("  admit             — admit the current helper lemma sub-proof (blocks Qed)")
        print("  status            — display current proof state")
        print("  tree              — display proof tree")
        print("  explain           — show agent reasoning trace")
        print("  help              — show this help")
        print("  quit              — exit")

    def _do_tree(self):
        print(visualizer.render_tree(self.controller.proof_tree))

    def _do_quit(self):
        self._save_readline_history()
        self._done = True

    ###########################
    ## Internal helpers      ##
    ###########################

    def _extract_and_reset_tactics(self) -> list:
        """Extract and pop pre-existing tactics so _init_proof_session sees a clean state."""
        coq = self.controller.coq
        proof = coq.get_unproven_proof()
        if not proof or len(proof.steps) <= 1:
            return []

        tactics = [step.text.strip() for step in proof.steps[1:]]

        for _ in range(len(tactics)):
            coq.proof_file.pop_step(proof)

        coq.proof = coq.get_unproven_proof()
        return tactics

    def _advance_one(self):
        if self._gen is None or self._done:
            return None
        try:
            result = next(self._gen)
            if result.get('type') == 'done' or result.get('proof_complete'):
                self._done = True
            return result
        except StopIteration:
            self._done = True
            return {'type': 'done', 'success': self.controller.is_successful}

    def _display_state(self):
        goals = self.controller.coq.get_goal_str() or ""
        print(visualizer.render_state(goals, open_lemmas=self.controller.helper_lemma_context()))

    def _report(self, result):
        # step_generator() already renders each action; only add what it omits.
        t = result.get('type')
        if t == 'tactic':
            if result.get('success'):
                if result.get('proof_complete'):
                    print("🎉 Proof complete!")
            else:
                print(f"  — {result.get('error', '')}")
        elif t == 'rollback':
            if not result.get('success'):
                print("Agent rollback failed.")
        elif t == 'done':
            if result.get('success'):
                print("🎉 Proof complete!")
            else:
                print("Session ended (max steps reached or proof failed).")
