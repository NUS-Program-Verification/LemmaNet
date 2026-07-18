#!/usr/bin/env python3
"""
Automated Coq proof validation - 2 steps:
1. Generate initial Coq goal and proof from source code
2. Generate helper lemmas based on WP goal
"""

import sys
import subprocess
import os
import re
import json
from pathlib import Path
from typing import Tuple, Dict, Optional

import litellm

litellm.drop_params = True  # drop unsupported params e.g. temperature


def get_library_paths_from_config(config_path: str) -> list:
    """
    Read library paths from a JSON config file and return coqc -R arguments.
    
    Config format: {"coq": {"library_paths": [{"path": "...", "name": "..."}, ...]}}
    Returns: list of strings like ["-R", "/path/to/lib", "libname", "-R", ...]
    """
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        lib_paths = config.get('coq', {}).get('library_paths', [])
        args = []
        for lib in lib_paths:
            args.extend(["-R", lib['path'], lib['name']])
        return args
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return []


class GPTClient:
    """LiteLLM API client with message threading and token tracking."""
    
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 api_base: Optional[str] = None):
        raw_model = model or os.getenv("LLM_MODEL", "openai/gpt-5.2-2025-12-11")
        # Normalize with provider prefix. Default to OpenAI, matching proof-search.
        self.model = raw_model if '/' in raw_model else f'openai/{raw_model}'
        self.api_key = api_key
        self.api_base = api_base
        self.messages = []
        
        # Token usage tracking
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.api_calls = 0
        
        # Initialize with system message
        system_msg = "You are an expert in Coq theorem proving and Frama-C/WP verification. Generate valid Coq code that compiles with coqc."
        self.messages.append({"role": "system", "content": system_msg})
    
    def generate(self, prompt: str) -> str:
        """
        Generate response from prompt, maintaining conversation history.
        Each call adds to the message thread and tracks token usage.
        """
        # Add user message to thread
        self.messages.append({"role": "user", "content": prompt})
        
        # Generate response with full conversation history.
        api_params = {
            "model": self.model,
            "messages": self.messages,
            "temperature": 0.0,
        }
        if self.api_key:
            api_params["api_key"] = self.api_key
        if self.api_base:
            api_params["api_base"] = self.api_base

        response = litellm.completion(**api_params)
        
        # Track token usage
        if response.usage:
            prompt_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(response.usage, "completion_tokens", 0) or 0
            total_tokens = getattr(response.usage, "total_tokens", None)
            if total_tokens is None:
                total_tokens = prompt_tokens + completion_tokens
            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.total_tokens += total_tokens
        self.api_calls += 1
        
        assistant_response = response.choices[0].message.content or ""
        
        # Add assistant response to thread
        self.messages.append({"role": "assistant", "content": assistant_response})
        
        return assistant_response
    
    def get_conversation_length(self) -> int:
        """Get number of messages in conversation."""
        return len(self.messages)
    
    def get_token_stats(self) -> Dict:
        """Get token usage statistics."""
        return {
            'prompt_tokens': self.total_prompt_tokens,
            'completion_tokens': self.total_completion_tokens,
            'total_tokens': self.total_tokens,
            'api_calls': self.api_calls
        }


class ProofValidator:
    """Validates Coq proofs with automatic retry."""
    
    MAX_ATTEMPTS = 5
    COQ_VERSION = "8.18.0"
    AUTOROCQ_DIR = os.path.join(os.path.dirname(__file__), "..")
    DEFAULT_LIB_PATH = ["-R", os.path.join(AUTOROCQ_DIR, "benchmarks", "AutoRocq-bench", "libautorocq"), "libframac"]
    
    def __init__(self, output_dir: str, gpt_client: GPTClient, lib_path: list = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.gpt = gpt_client
        self.log_buffer = []
        self.lib_path = lib_path if lib_path else self.DEFAULT_LIB_PATH
    
    def log(self, msg: str):
        """Log message."""
        self.log_buffer.append(msg)
        print(msg)
    
    def save_log(self, filename: str):
        """Save log to file."""
        (self.output_dir / filename).write_text('\n'.join(self.log_buffer))
    
    def extract_coq_code(self, response: str) -> str:
        """
        Extract pure Coq code from GPT response.
        Handles explanatory text, markdown blocks, various formats.
        """
        # Try markdown code blocks first (case-insensitive for Coq/coq)
        matches = re.findall(r'```(?:coq|Coq)?\s*\n(.*?)```', response, re.DOTALL | re.IGNORECASE)
        if matches:
            return max(matches, key=len).strip()
        
        return response.strip()
    
    def compile_coq_file(self, filepath: str) -> Tuple[bool, str]:
        """Compile Coq file with library path."""
        cmd = ['coqc', *self.lib_path, os.path.abspath(filepath)]
        self.log(f"Compile: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            return (result.returncode == 0, result.stderr)
        except Exception as e:
            return (False, str(e))
    
    def generate_with_retry(self, initial_prompt: str, output_file: Path, step_name: str, 
                           return_response: bool = False) -> Tuple[str, str]:
        """
        Generate Coq code with automatic retry on compilation errors.
        Maintains conversation thread across retries.
        
        Args:
            initial_prompt: The initial prompt to GPT
            output_file: Where to save the generated code
            step_name: Name of the step (for logging)
            return_response: If True, return (file_path, response), else (file_path, "")
            
        Returns:
            (file_path, final_response) tuple
        """
        current_prompt = initial_prompt
        
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            self.log(f"\n--- {step_name} ATTEMPT {attempt}/{self.MAX_ATTEMPTS} ---")
            self.log(f"Conversation messages: {self.gpt.get_conversation_length()}")
            self.log(f"Prompt:\n{current_prompt}\n")
            
            response = self.gpt.generate(current_prompt)
            self.log(f"Response:\n{response}\n")
            
            coq_code = self.extract_coq_code(response)
            output_file.write_text(coq_code)
            self.log(f"Generated: {output_file.name} ({len(coq_code)} bytes)")
            
            if "\nSection " in coq_code or "\nModule " in coq_code:
                current_prompt = f"The previous Coq code contains sections or modules. Remove them and try again."
                continue
            
            if "\nadmit" in coq_code or "\nAdmitted." in coq_code:
                current_prompt = f"The previous Coq code contains incomplete proofs with admits. Always write a complete proof for each lemma."
                continue
            
            # Try to compile
            success, compile_error = self.compile_coq_file(str(output_file))
            
            if success:
                self.log(f"{step_name} successful on attempt {attempt}")
                return (str(output_file), response if return_response else "")
            
            # Failed - log error
            self.log(f"Compilation failed:\n{compile_error}")
            
            if attempt == self.MAX_ATTEMPTS:
                raise Exception(f"{step_name} FAILED: No compilable code after {self.MAX_ATTEMPTS} attempts")
            
            # Create fix prompt for next attempt
            current_prompt = f"""The previous Coq code had compilation errors. Fix them.

PREVIOUS CODE:
{coq_code}

COMPILATION ERROR:
{compile_error}

Fix the errors to make it compile with coqc.
If you are not able to resolve the errors, try to reduce the code to minimal working lemmas/proofs.

Common errors:
- Invalid Coq syntax (e.g. no ACSL syntax such as \\forall or \\separated)
- Incorrect function application: f x y (NOT f(x,y))
- Missing necessary libraries: e.g., "Require Import Lia." for error "reference not found"
- Missing necessary scopes: e.g., "Open Scope bool_scope." for error "unknown interpretation '_ && _'"

Provide ONLY the corrected Coq code."""
    
    def generate_initial_proof(self, source_code: str, name: str) -> str:
        """
        Step 1: Generate simplified initial proof.
        """
        self.log("\n" + "="*80)
        self.log("STEP 1: Generate Initial Proof")
        self.log("="*80)
        
        prompt = f"""Please analyze the annotated source code and encode the property to prove as a Coq goal.

{source_code}

YOUR TASK:
Write a Coq-{self.COQ_VERSION} .v file with a proved goal that captures what the annotation states.

CRITICAL REQUIREMENTS FOR COQ-{self.COQ_VERSION}:
- Focus only on the highlighted property to prove 
- Use ONLY valid Coq syntax (NO ACSL: no \\forall integer, \\separated, \\valid, array[i])
- Proper Coq function application: "f x y z" NOT "f(x, y, z)"
- Import necessary libraries: "Require Import Lia." for arithmetic
- Open scopes if needed: "Open Scope Z_scope." for integers
- The code MUST compile with coqc

Provide ONLY the complete Coq .v file code."""
        
        file_path, _ = self.generate_with_retry(
            prompt,
            self.output_dir / f"ghost_vc.v",
            "STEP 1",
            return_response=False
        )
        return file_path
    
    def generate_helper_lemmas(self, wp_goal: str, name: str, success: bool) -> Tuple[str, str]:
        """
        Step 2: Generate helper lemmas.
        """
        self.log("\n" + "="*80)
        self.log("STEP 2: Generate Helper Lemmas")
        self.log("="*80)
        
        if success:
            prompt = "The Coq code compiled successfully: Coq lemma proved! "
        else:
            prompt = "Max attempts reached: Coq lemma is not proved! "
        
        prompt += f"""Now I will provide you an *equivalent* Coq goal, that has been directly discharged from Frama-C/WP using the same annotation (provided earlier). Please analyze this theorem and the previous Coq lemma (with proof), and propose *strong enough helper lemmas* to help prove the WP goal. You don't need to prove the WP goal, just propose relevant helper lemmas that can be useful.

DISCHARGED WP GOAL FROM FRAMA-C:
{wp_goal}

YOUR TASK:
1. Study the WP goal carefully:
   - What the low-level encoding looks like
   - What makes the proof difficult
   - How to bridge this WP goal to the simpler Coq lemma

2. During proving, you may refer to the successfully compiled Coq code you already generated

3. Generate helper lemmas that:
   - Can be useful to prove the simplified Coq lemma
   - Bridge the gap between Frama-C/Why3 encoding and the simpler Coq lemma
   - Provide key insights/results that may make the WP goal easier to prove

CRITICAL REQUIREMENTS:
- Use ONLY valid Coq syntax
- You don't need to prove the WP goal yet
- For each helper lemma proposed, provide its proof 
- Helper lemmas can be very specific to the WP goal (e.g., using constants from it)
- Do NOT provide trivial helper lemmas like "forall P Q : Prop, P /\ Q -> P."
- Do NOT wrap your definitions and helper lemmas in Section/Module.

Provide ONLY the complete Coq .v file containing helper lemmas."""
        
        # Try to generate compilable helper lemmas
        code_compiled = False
        v2_file_path = str(self.output_dir / f"ghost_vc_helper_lemmas.v")
        
        try:
            v2_file_path, _ = self.generate_with_retry(
                prompt,
                self.output_dir / f"ghost_vc_helper_lemmas.v",
                "STEP 2 CODE",
                return_response=False
            )
            code_compiled = True
        except Exception as e:
            self.log(f"\nHelper lemmas did not compile after {self.MAX_ATTEMPTS} attempts")
            self.log("Proceeding to generate proof plan anyway...")
        
        # Always generate proof plan (even if code didn't compile)
        self.log("\n--- STEP 2: Requesting Proof Plan ---")
        
        plan_prompt = "The helper lemmas compiled successfully!\n" if code_compiled else "The helper lemmas did not compile, proceeding...\n"
        
        
        plan_prompt += """Now, provide a detailed step-by-step proof plan explaining how to use these helper lemmas to prove the original WP goal.

Your proof plan should:
- Explain the overall proof strategy
- Describe a step-by-step plan including where each helper lemma can be applied
- Identify any remaining challenges
- Be concise and to the point

Provide the proof plan in plain text."""
        
        self.log(f"Plan prompt:\n{plan_prompt}\n")
        plan_response = self.gpt.generate(plan_prompt)
        self.log(f"Plan response:\n{plan_response}\n")
        
        # Save proof plan
        proof_plan = re.sub(r'```.*?```', '', plan_response, flags=re.DOTALL).strip()
        
        plan_file = self.output_dir / f"proof_plan.txt"
        plan_file.write_text(proof_plan)
        self.log(f"Saved proof plan: {plan_file.name} ({len(proof_plan)} bytes)")
        
        # Raise exception after saving plan if code didn't compile
        if not code_compiled:
            raise Exception("STEP 2: Helper lemmas did not compile (proof plan saved)")
        
        return v2_file_path, str(plan_file)
    
    def run_validation(self, source_code: str, wp_goal: str, proof_name: str):
        """Run complete validation workflow."""
        self.log("\n" + "="*80)
        self.log("VALIDATION WORKFLOW")
        self.log("="*80)
        
        step1_failed = False
        step2_failed = False
        
        # Step 1: Try to generate initial proof
        try:
            v1_path = self.generate_initial_proof(source_code, proof_name)
            self.log("\nStep 1 completed successfully")
        except Exception as e:
            self.log(f"\nStep 1 failed after max retries: {e}")
            self.log("Continuing to Step 2 anyway...")
            step1_failed = True
        
        # Step 2: Always attempt, even if Step 1 failed
        try:
            v2_path, plan_path = self.generate_helper_lemmas(wp_goal, proof_name, not step1_failed)
            self.log("\nStep 2 completed successfully")
        except Exception as e:
            self.log(f"\nStep 2 failed: {e}")
            step2_failed = True
            
        # Always log and save token usage
        stats = self.gpt.get_token_stats()
        self.log("\n" + "="*80)
        self.log("TOKEN USAGE STATISTICS")
        self.log("="*80)
        self.log(f"API Calls: {stats['api_calls']}")
        self.log(f"Prompt Tokens: {stats['prompt_tokens']:,}")
        self.log(f"Completion Tokens: {stats['completion_tokens']:,}")
        self.log(f"Total Tokens: {stats['total_tokens']:,}")
        self.log("="*80)
        
        # Save token stats to separate file
        stats_file = self.output_dir / "token_usage.json"
        stats_file.write_text(json.dumps(stats, indent=2))
        
        self.save_log(f"ghost_vc_log.txt")
        
        if step1_failed and step2_failed:
            raise Exception("Both steps failed - no compilable code generated")
        elif step1_failed:
            raise Exception("Ghost VC not proved")
        elif step2_failed:
            raise Exception("Helper lemmas not generated")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Coq proof validation with LiteLLM")
    parser.add_argument('--source', required=True, help='Source file')
    parser.add_argument('--wp-goal', required=True, help='WP goal .v file')
    parser.add_argument('--base-name', default='proof', help='Output base name')
    parser.add_argument('--output-dir', default='.', help='Output directory')
    parser.add_argument('--api-key', help='LLM API key. LiteLLM can also read provider-specific environment variables.')
    parser.add_argument('--api-base', help='Optional LiteLLM API base URL')
    parser.add_argument('--model', help='LiteLLM model name. Defaults to LLM_MODEL or openai/gpt-4.1')
    parser.add_argument('--config', help='Path to JSON config file with Coq library paths')
    try:
        args = parser.parse_args()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Determine library paths from config file
    lib_path = get_library_paths_from_config(args.config) if args.config else None
    
    # Read inputs
    source_code = Path(args.source).read_text(errors='ignore')
    wp_goal_full = Path(args.wp_goal).read_text(errors='ignore')
    
    # Extract goal portion
    if '(* Why3 goal *)' in wp_goal_full:
        wp_goal = wp_goal_full.split('(* Why3 goal *)')[-1]
    elif 'Open Scope Z_scope.' in wp_goal_full:
        wp_goal = wp_goal_full.split('Open Scope Z_scope.')[-1]
    else:
        wp_goal = wp_goal_full
    
    # Run validation
    gpt = GPTClient(api_key=args.api_key, model=args.model, api_base=args.api_base)
    validator = ProofValidator(args.output_dir, gpt, lib_path)
    
    try:
        validator.run_validation(source_code, wp_goal, args.base_name)
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        print(f"Check: {args.output_dir}/{args.base_name}_log.txt", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
