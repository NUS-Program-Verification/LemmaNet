#!/usr/bin/env python3
"""Script to run proof search on benchmark files with optional Ghost-VC preprocessing."""

import subprocess
import sys
import shutil
import argparse
import re
import json
from typing import Set
from pathlib import Path


# =============================================================================
# File I/O Utilities
# =============================================================================

def read_file_lines(file_path: Path) -> list[str]:
    with open(file_path, 'r') as f:
        return f.readlines()

def read_file_content(file_path: Path) -> str:
    with open(file_path, 'r') as f:
        return f.read()

def write_file_content(file_path: Path, content: str) -> None:
    with open(file_path, 'w') as f:
        f.write(content)

def write_file_lines(file_path: Path, lines: list[str]) -> None:
    with open(file_path, 'w') as f:
        f.writelines(lines)


# =============================================================================
# Paths and Configuration
# =============================================================================

AUTOROCQ_PP_ROOT = Path(__file__).parent.parent
PROOF_SEARCH_DIR = AUTOROCQ_PP_ROOT / "proof-search"
CONFIG_FILE = PROOF_SEARCH_DIR / "configs" / "default_config.json"
WORK_DIR = AUTOROCQ_PP_ROOT / "temp"
SVCOMP_ROOT = AUTOROCQ_PP_ROOT / "benchmarks" / "AutoRocq-bench" / "benchmarks" / "svcomp"
NTP4VC_ROOT = AUTOROCQ_PP_ROOT / "benchmarks" / "ntp4vc"

DEFAULT_OUTPUT_DIR = AUTOROCQ_PP_ROOT / "out"

BENCHMARK_CONFIGS = {
    "svcomp-ablation": {
        "root": SVCOMP_ROOT,
        "file": AUTOROCQ_PP_ROOT / "benchmarks" / "svcomp-ablation.txt",
        "gvc_result_dir": AUTOROCQ_PP_ROOT / "offline-lemma" / "svcomp",
        "config_type": "shared",
    },
    "svcomp-remaining": {
        "root": SVCOMP_ROOT,
        "file": AUTOROCQ_PP_ROOT / "benchmarks" / "svcomp-remaining.txt",
        "gvc_result_dir": AUTOROCQ_PP_ROOT / "offline-lemma" / "svcomp",
        "config_type": "shared",
    },
    "ntp4vc-ablation": {
        "root": NTP4VC_ROOT,
        "file": AUTOROCQ_PP_ROOT / "benchmarks" / "ntp4vc-ablation.txt",
        "gvc_result_dir": AUTOROCQ_PP_ROOT / "offline-lemma" / "ntp4vc",
        "config_type": "per_file",
    },
    "ntp4vc-remaining": {
        "root": NTP4VC_ROOT,
        "file": AUTOROCQ_PP_ROOT / "benchmarks" / "ntp4vc-remaining.txt",
        "gvc_result_dir": AUTOROCQ_PP_ROOT / "offline-lemma" / "ntp4vc",
        "config_type": "per_file",
    },
}

# =============================================================================
# Coq Constants
# =============================================================================

GVC_FILENAME = "ghost_vc.v"
HL_FILENAME = "ghost_vc_helper_lemmas.v"
PLAN_FILENAME = "proof_plan.txt"

# Coq declaration keywords
DECLARATION_KEYWORDS = [
    'Parameter', 'Axiom', 'Definition', 'Inductive', 'CoInductive',
    'Fixpoint', 'CoFixpoint', 'Record', 'Structure', 'Class', 'Instance',
    'Theorem', 'Lemma', 'Corollary', 'Proposition', 'Remark', 'Fact',
    'Example', 'Goal'
]

# Keywords that typically have proofs (end with Qed/Defined/Admitted)
PROOF_KEYWORDS = {'Lemma', 'Theorem', 'Corollary', 'Proposition', 'Fact', 
                  'Remark', 'Example', 'Goal'}

# Regex patterns for declaration extraction
DECLARATION_PATTERNS = [rf'^\s*{keyword}\s+(\w+)' for keyword in DECLARATION_KEYWORDS]

# Standard WP/Frama-C memory model identifiers
STANDARD_WP_IDENTIFIERS = {
    # Memory model types
    'addr', 'malloc', 'table',
    
    # Memory operations
    'offset', 'base', 'shift', 'null', 'global',
    
    # Memory predicates
    'addr_le', 'addr_lt', 'addr_le_bool', 'addr_lt_bool',
    'valid_rw', 'valid_rd', 'valid_obj', 'invalid',
    'included', 'separated',
    
    # Memory model functions
    'region', 'linked', 'static_malloc', 'statically_allocated',
    'int_of_addr', 'addr_of_int',
    'table_of_base', 'table_to_offset',
    
    # Integer types and operations
    'is_bool', 'is_uint8', 'is_sint8', 'is_uint16', 'is_sint16',
    'is_uint32', 'is_sint32', 'is_uint64', 'is_sint64',
    'is_uint', 'is_sint',
    'to_bool', 'to_uint8', 'to_sint8', 'to_uint16', 'to_sint16',
    'to_uint32', 'to_sint32', 'to_uint64', 'to_sint64',
    'to_uint', 'to_sint', 'two_power_abs',
    
    # Comparison and logic
    'eqb', 'neqb', 'zlt', 'zleq', 'rlt', 'rleq',
    
    # Bitwise operations
    'land', 'lor', 'lxor', 'lnot', 'lsl', 'lsr',
    'bit_test', 'bit_testb',
    
    # Memory arrays and initialization
    'IsArray_uint8', 'sconst', 'framed', 'eqmem', 'havoc',
    'cinits', 'is_init_range', 'set_init', 'monotonic_init',
    
    # Real numbers
    'real_of_int',
}

# =============================================================================
# Coq Functions
# =============================================================================

def rename_identifier_in_content(content: str, old_name: str, new_name: str) -> str:
    pattern = r'\b' + re.escape(old_name) + r'\b'
    return re.sub(pattern, new_name, content)


def is_coq_header_line(line: str) -> bool:
    """
    Check if a line is a Coq header statement (imports, requires, scopes, etc.).
    """
    stripped = line.strip()
    if not stripped:
        return False
    
    # Common Coq header patterns
    header_patterns = [
        'Require',       # Require Import X, Require X
        'From',          # From X Require Import Y
        'Import',        # Import X
        'Export',        # Export X
        'Open Scope',    # Open Scope X_scope
        'Close Scope',   # Close Scope X_scope
        'Set',           # Set Printing All, etc.
        'Unset',         # Unset Printing All, etc.
        'Local',         # Local Set/Unset
        'Global',        # Global Set/Unset
    ]
    
    return any(stripped.startswith(pattern) for pattern in header_patterns)


def normalize_coq_statement(line: str) -> str:
    """
    Normalize a Coq statement for comparison (remove extra whitespace).
    """
    return ' '.join(line.strip().split())


def extract_coq_declarations(lines: list, start_idx: int = 0, end_idx: int = None) -> list[dict]:
    """Extract all declarations (lemmas, theorems, definitions, etc.) from Coq code."""
    if end_idx is None:
        end_idx = len(lines)
    
    declarations = []
    i = start_idx
    
    while i < end_idx:
        line = lines[i].strip()
        
        # Check if line starts with a declaration keyword
        found_keyword = None
        for keyword in DECLARATION_KEYWORDS:
            if line.startswith(keyword + ' '):
                found_keyword = keyword
                break
        
        if found_keyword:
            # Extract the name
            name_match = re.match(rf'{found_keyword}\s+(\w+)', line)
            if name_match:
                name = name_match.group(1)
                start_line = i
                end_line = i
                
                # For declarations that have proofs, find the Qed/Defined/Admitted/Abort
                if found_keyword in PROOF_KEYWORDS:
                    # Look for the proof terminator
                    for j in range(i, end_idx):
                        current_line = lines[j].strip()
                        if re.search(r'\b(Qed|Defined|Admitted|Abort)\s*\.', current_line):
                            end_line = j
                            break
                        # Also handle inline proofs like "Proof. auto. Qed."
                        if j == i and ('Qed' in current_line or 'Abort' in current_line):
                            end_line = j
                            break
                    else:
                        # No terminator found, use the whole file rest
                        end_line = end_idx - 1
                else:
                    # For non-proof declarations (Definition, Parameter, etc.)
                    for j in range(i, end_idx):
                        current_line = lines[j].strip()
                        if current_line.endswith('.'):
                            end_line = j
                            break
                
                content = ''.join(lines[start_line:end_line + 1])
                
                declarations.append({
                    'name': name,
                    'start_line': start_line,
                    'end_line': end_line,
                    'type': found_keyword,
                    'content': content
                })
                
                i = end_line + 1
                continue
        
        i += 1
    
    return declarations


def extract_declared_names(lines: list, start_idx: int = 0, end_idx: int = None) -> Set[str]:
    declarations = extract_coq_declarations(lines, start_idx, end_idx)
    return {decl['name'] for decl in declarations}


def extract_all_declared_names(lines: list, start_idx: int = 0, end_idx: int = None) -> Set[str]:
    """Extract all declared names including Record/Inductive constructors."""
    declarations = extract_coq_declarations(lines, start_idx, end_idx)
    names = set()
    for decl in declarations:
        names.add(decl['name'])
        content = decl['content']
        
        if decl['type'] in ('Record', 'Structure'):
            constructor_match = re.search(r':=\s*(\w+)\s*\{', content)
            if constructor_match:
                names.add(constructor_match.group(1))
        elif decl['type'] in ('Inductive', 'CoInductive'):
            constructor_matches = re.findall(r'\|\s*(\w+)', content)
            names.update(constructor_matches)
    
    return names

def extract_coq_declarations_from_file(file_path: Path) -> list[dict]:
    lines = read_file_lines(file_path)
    return extract_coq_declarations(lines)


def get_library_paths_from_config(config_path: Path) -> list[dict]:
    """
    Read library paths from a config file.
    
    Args:
        config_path: Path to the JSON config file
    
    Returns:
        List of dicts with 'path' and 'name' keys
    """
    if not config_path.exists():
        return []
    
    with open(config_path, 'r') as f:
        config = json.load(f)
    
    return config.get('coq', {}).get('library_paths', [])


def compile_coq_file(coq_file: Path, config_path: Path = None) -> tuple[bool, str]:
    """
    Try to compile a Coq file using coqc.
    
    Args:
        coq_file: Path to the .v file to compile
        config_path: Path to config file with library paths (optional)
    
    Returns:
        Tuple of (success: bool, error_output: str)
    """
    cmd = ["coqc"]
    
    # Add library paths from config
    if config_path:
        library_paths = get_library_paths_from_config(config_path)
        for lib in library_paths:
            cmd.extend(["-R", lib['path'], lib['name']])
    else:
        # Fallback: default library path
        cmd.extend(["-R", str(AUTOROCQ_PP_ROOT / "benchmarks" / "AutoRocq-bench" / "libautorocq"), "libframac"])

    cmd.append(str(coq_file))
    
    result = subprocess.run(
        cmd,
        cwd=str(PROOF_SEARCH_DIR),
        capture_output=True,
        text=True
    )
    
    return result.returncode == 0, result.stderr


def merge_coq_file(vc_file_path: Path, hl_file_path: Path) -> Set[str]:
    """
    Merge the Coq file with the helper lemmas.
    Detects naming collisions and renames conflicting identifiers in the helper lemmas file.
    Then merges headers and inserts helper lemmas before the final theorem.
    Returns the set of HL declaration names that were added to the merged file.
    """
    vc_content = read_file_content(vc_file_path)
    hl_content = read_file_content(hl_file_path)
    
    vc_lines = vc_content.split('\n')
    hl_lines = hl_content.split('\n')
    
    # Find the end of header section in VC file
    # Header includes initial comments, Require, Import, Open Scope, etc.
    vc_header_end = 0
    for i, line in enumerate(vc_lines):
        stripped = line.strip()
        if is_coq_header_line(line) or not stripped or stripped.startswith('(*'):
            vc_header_end = i + 1
        else:
            # Found first non-header, non-comment, non-empty line
            break
    
    # Find the start of the last theorem/lemma in VC file
    theorem_start = -1
    for i in range(len(vc_lines) - 1, -1, -1):
        stripped = vc_lines[i].strip()
        if stripped.startswith(('Theorem ', 'Lemma ', 'Goal ', 'Example ')):
            theorem_start = i
            break
    
    if theorem_start == -1:
        print(f"Warning: No theorem/goal found in {vc_file_path}")
        return set()
    
    # Extract header statements from HL file
    hl_header_lines = []
    hl_content_start = 0
    in_header_comment = False
    
    for i, line in enumerate(hl_lines):
        stripped = line.strip()
        
        # Track multi-line comments in header section
        if '(*' in stripped:
            in_header_comment = True
        if '*)' in stripped:
            in_header_comment = False
            if '(*' not in stripped:
                # This line only closes comment, not starts one
                hl_content_start = i + 1
                continue
        
        # Skip lines inside header comments
        if in_header_comment:
            continue
            
        # Skip empty lines
        if not stripped:
            hl_content_start = i + 1
            continue
        
        # Collect header lines
        if is_coq_header_line(line):
            hl_header_lines.append(line)
            hl_content_start = i + 1
        else:
            # Found first non-header content
            hl_content_start = i
            break
    
    # STEP 1: Extract declared names from both files (including constructors)
    vc_names = extract_all_declared_names(vc_lines, vc_header_end, theorem_start)
    hl_names = extract_all_declared_names(hl_lines, hl_content_start)
    
    # STEP 1b: Detect if VC file imports specific modules (e.g. in ntp4vc files)
    imports_memory_module = False
    imports_stdpp_decidable = False
    for line in vc_lines[:vc_header_end]:
        if 'Memory.Memory' in line or 'Why3.Memory' in line:
            imports_memory_module = True
        if 'stdpp' in line and 'decidable' in line:
            imports_stdpp_decidable = True
    
    MEMORY_MODULE_IDENTIFIERS = {'addr', 'shift', 'base', 'region', 'offset', 
                                  'separated', 'included', 'valid_rw', 'valid_rd'}
    
    STDPP_IDENTIFIERS = {'decide'}
    
    # STEP 2: Find collisions and categorize them
    collisions = vc_names.intersection(hl_names)
    
    module_collisions = set()
    if imports_memory_module:
        module_collisions = module_collisions.union(MEMORY_MODULE_IDENTIFIERS.intersection(hl_names))
    if imports_stdpp_decidable:
        module_collisions = module_collisions.union(STDPP_IDENTIFIERS.intersection(hl_names))
    collisions = collisions.union(module_collisions)
    
    standard_collisions = collisions.intersection(STANDARD_WP_IDENTIFIERS)
    standard_collisions = standard_collisions.union(module_collisions)
    custom_collisions = collisions - standard_collisions
    
    if collisions:
        print(f"    {len(collisions)} naming collision(s): {len(standard_collisions)} removed, {len(custom_collisions)} renamed")
    
    # STEP 3a: Filter out problematic content from HL
    # - Standard collision declarations (already defined in VC)
    # - ZArith imports that can re-introduce shadowing (but keep Lia, etc.)
    # - Open Scope Z_scope (already open in VC)
    # - Section/End blocks (can introduce incompatible types)
    
    hl_lemmas_lines = []
    i = hl_content_start
    skip_until_period = False
    in_section = 0  # Track nested sections
    
    while i < len(hl_lines):
        line = hl_lines[i]
        stripped = line.strip()
        
        # If we're skipping until end of declaration
        if skip_until_period:
            if stripped.endswith('.'):
                skip_until_period = False
            i += 1
            continue
        
        # Handle Require/From lines - skip those that import ZArith (causes shadowing of 'shift')
        if stripped.startswith('Require') or stripped.startswith('From'):
            if 'ZArith' in stripped:
                # Parse imports and filter out ZArith-related ones
                # Handle both "Require Import X Y." and "From Coq Require Import X Y."
                match = re.match(r'^(From\s+\S+\s+)?Require\s+Import\s+(.*)\.', stripped)
                if match:
                    prefix = match.group(1) or ''  # "From Coq " or empty
                    imports = match.group(2).split()
                    filtered = [imp for imp in imports if 'ZArith' not in imp]
                    if filtered:
                        hl_lemmas_lines.append(f"{prefix}Require Import {' '.join(filtered)}.\n")
                    i += 1
                    continue
                # If it's a pure ZArith import, skip entirely
                i += 1
                continue
        
        # Track and skip Section blocks (they can introduce incompatible types)
        if stripped.startswith('Section '):
            in_section += 1
            i += 1
            continue
        if stripped.startswith('End ') and in_section > 0:
            in_section -= 1
            i += 1
            continue
        if in_section > 0:
            i += 1
            continue
        
        # Check if this line starts a declaration that should be skipped (standard collisions)
        should_skip = False
        for pattern in DECLARATION_PATTERNS:
            match = re.match(pattern, line)
            if match:
                declaration_name = match.group(1)
                if declaration_name in standard_collisions:
                    should_skip = True
                    if not stripped.endswith('.'):
                        skip_until_period = True
                    break
        
        if should_skip:
            i += 1
            continue
        
        # Include this line
        hl_lemmas_lines.append(line)
        i += 1
    
    # STEP 3b: Remove LEMMAS that use incompatible function signatures or type predicates.
    
    INCOMPATIBLE_FUNCTIONS = {'shift', 'null',
                               'to_uint8', 'to_sint8', 'to_uint16', 'to_sint16',
                               'to_uint32', 'to_sint32', 'to_uint64', 'to_sint64'}
    
    INCOMPATIBLE_TYPE_PREDICATES = {'is_sint8', 'is_uint8', 'is_sint16', 'is_uint16',
                                     'is_sint32', 'is_uint32', 'is_sint64', 'is_uint64',
                                     'is_bool', 'is_sint', 'is_uint'}
    
    LEMMA_TYPES = {'Lemma', 'Corollary', 'Proposition'}
    
    all_incompatible = INCOMPATIBLE_FUNCTIONS.union(INCOMPATIBLE_TYPE_PREDICATES)
    
    if imports_stdpp_decidable:
        all_incompatible = all_incompatible.union(STDPP_IDENTIFIERS)
    
    incompatible_used = standard_collisions.intersection(all_incompatible)
    
    if incompatible_used:
        hl_content = '\n'.join(hl_lemmas_lines)
        hl_decls = extract_coq_declarations(hl_content.split('\n'))  # Only helper lemmas
        
        decls_to_remove = set()
        for decl in hl_decls:
            if decl['type'] not in LEMMA_TYPES:
                continue
            for func in incompatible_used:
                # Check if the incompatible function appears in the declaration body
                if re.search(r'\b' + re.escape(func) + r'\b', decl['content']):
                    decls_to_remove.add(decl['name'])
                    break
        
        if decls_to_remove:
            # Also remove dependents of the incompatible lemmas
            dependencies = find_dependencies(hl_decls)
            all_to_remove = compute_transitive_dependents(dependencies, decls_to_remove)
            
            temp_lines = hl_content.split('\n')
            lines_to_remove = set()
            for decl in hl_decls:
                if decl['name'] in all_to_remove:
                    for line_num in range(decl['start_line'], decl['end_line'] + 1):
                        lines_to_remove.add(line_num)
            hl_lemmas_lines = [line for i, line in enumerate(temp_lines) if i not in lines_to_remove]
            if len(all_to_remove) > len(decls_to_remove):
                print(f"    Removed {len(decls_to_remove)} lemmas with incompatible signatures/types (+{len(all_to_remove) - len(decls_to_remove)} dependents)")
            else:
                print(f"    Removed {len(decls_to_remove)} lemmas with incompatible signatures/types")
    
    # Rename custom collisions
    if custom_collisions:
        hl_content = '\n'.join(hl_lemmas_lines)
        for name in custom_collisions:
            hl_content = rename_identifier_in_content(hl_content, name, f"hl_{name}")
        hl_lemmas_lines = hl_content.split('\n')
    
    # STEP 3c: Remove lemmas that end with Admitted (unproved helper lemmas)
    hl_lemmas_lines, admitted_removed = filter_admitted_lemmas(hl_lemmas_lines)
    if admitted_removed:
        print(f"    Removed {len(admitted_removed)} admitted (unproved) lemmas")
    
    # Remove trailing empty lines
    while hl_lemmas_lines and not hl_lemmas_lines[-1].strip():
        hl_lemmas_lines.pop()
    
    # STEP 4: Merge headers (avoid duplicates)
    vc_header_lines = vc_lines[:vc_header_end]
    merged_header = vc_header_lines.copy()
    
    vc_header_normalized = set()
    for vc_line in vc_header_lines:
        if is_coq_header_line(vc_line):
            vc_header_normalized.add(normalize_coq_statement(vc_line))
    
    for hl_line in hl_header_lines:
        if is_coq_header_line(hl_line):
            stripped_hl = hl_line.strip()
            # Skip ZArith imports when Memory module is imported (shadows Memory.shift)
            if imports_memory_module and 'ZArith' in stripped_hl and \
               (stripped_hl.startswith('Require') or stripped_hl.startswith('From')):
                # Filter out ZArith-related imports
                match = re.match(r'^(From\s+\S+\s+)?Require\s+Import\s+(.*)\.', stripped_hl)
                if match:
                    prefix = match.group(1) or ''
                    imports = match.group(2).split()
                    filtered = [imp for imp in imports if 'ZArith' not in imp]
                    if not filtered:
                        continue  # Skip entirely if only ZArith imports
                    hl_line = f"{prefix}Require Import {' '.join(filtered)}.\n"
            normalized = normalize_coq_statement(hl_line)
            if normalized not in vc_header_normalized:
                merged_header.append(hl_line)
                vc_header_normalized.add(normalized)
    
    # STEP 5: Build merged content
    merged_lines = merged_header
    merged_lines.extend(vc_lines[vc_header_end:theorem_start])
    
    # Remove trailing empty lines before adding lemmas
    while merged_lines and not merged_lines[-1].strip():
        merged_lines.pop()
    
    # Count how many lemmas/declarations are being added
    final_hl_content = '\n'.join(hl_lemmas_lines)
    final_decls = extract_coq_declarations(final_hl_content.split('\n'))
    if final_decls:
        print(f"    Added {len(final_decls)} declarations from helper lemmas")
    else:
        print(f"    No declarations added from helper lemmas")
    
    merged_lines.append('')
    merged_lines.extend(hl_lemmas_lines)
    merged_lines.append('')
    merged_lines.extend(vc_lines[theorem_start:])
    
    # Write merged content
    merged_content = '\n'.join(merged_lines)
    write_file_content(vc_file_path, merged_content)
    
    return {d['name'] for d in final_decls}


def find_dependencies(declarations: list[dict]) -> dict[str, set[str]]:
    """
    Find dependencies between declarations.
    
    Returns:
        Dict mapping each declaration name to a set of names it depends on
    """
    dependencies = {}
    all_names = {decl['name'] for decl in declarations}
    
    for decl in declarations:
        decl_deps = set()
        content = decl['content']
        
        # Find all identifiers used in this declaration
        for name in all_names:
            if name != decl['name']:
                # Use word boundary regex to avoid partial matches
                if re.search(r'\b' + re.escape(name) + r'\b', content):
                    decl_deps.add(name)
        
        dependencies[decl['name']] = decl_deps
    
    return dependencies


def compute_transitive_dependents(dependencies: dict[str, set[str]], start_names: set[str]) -> set[str]:
    """
    Compute all declarations that transitively depend on the given names.
    
    Args:
        dependencies: Dict mapping each declaration to its direct dependencies
        start_names: Initial set of declaration names
    
    Returns:
        Set containing start_names and all declarations that depend on them
    """
    result = start_names.copy()
    changed = True
    
    while changed:
        changed = False
        for name, deps in dependencies.items():
            if name not in result and result.intersection(deps):
                result.add(name)
                changed = True
    
    return result


def filter_admitted_lemmas(lines: list[str], start_idx: int = 0) -> tuple[list[str], set[str]]:
    """
    Filter out lemmas/theorems that end with Admitted from the content.
    
    Args:
        lines: List of lines to filter
        start_idx: Starting index to begin filtering
    
    Returns:
        Tuple of (filtered lines, set of removed lemma names)
    """
    declarations = extract_coq_declarations(lines, start_idx)
    
    # Find lemmas that end with Admitted
    admitted_names = set()
    for decl in declarations:
        if decl['type'] in PROOF_KEYWORDS:
            # Check if the declaration ends with Admitted
            content = decl['content']
            if re.search(r'\bAdmitted\s*\.\s*$', content):
                admitted_names.add(decl['name'])
    
    if not admitted_names:
        return lines, set()
    
    # Compute transitive dependents
    dependencies = find_dependencies(declarations)
    all_to_remove = compute_transitive_dependents(dependencies, admitted_names)
    
    # Build set of lines to remove
    lines_to_remove = set()
    for decl in declarations:
        if decl['name'] in all_to_remove:
            for line_num in range(decl['start_line'], decl['end_line'] + 1):
                lines_to_remove.add(line_num)
    
    # Filter lines
    filtered_lines = [line for i, line in enumerate(lines) if i not in lines_to_remove]
    
    return filtered_lines, all_to_remove


def remove_declaration_and_dependents(file_path: Path, declarations: list[dict], 
                                     dependencies: dict[str, set[str]], 
                                     problem_name: str) -> set[str]:
    """Remove a declaration and all declarations that depend on it. Returns set of removed names."""
    to_remove = compute_transitive_dependents(dependencies, {problem_name})
    lines = read_file_lines(file_path)
    
    lines_to_remove = set()
    for decl in declarations:
        if decl['name'] in to_remove:
            for line_num in range(decl['start_line'], decl['end_line'] + 1):
                lines_to_remove.add(line_num)
    
    filtered_lines = [line for i, line in enumerate(lines) if i not in lines_to_remove]
    write_file_lines(file_path, filtered_lines)
    return to_remove


def iteratively_fix_file(file_path: Path, config_path: Path = None,
                         max_iterations: int = 10,
                         removable_names: Set[str] = None) -> bool:
    """
    Iteratively compile a file, removing problematic declarations until it compiles.
    If removable_names is provided, only declarations in that set may be removed.
    """
    for _ in range(max_iterations):
        success, error_output = compile_coq_file(file_path, config_path)
        if success:
            return True
        
        declarations = extract_coq_declarations_from_file(file_path)
        if not declarations:
            return False
        
        dependencies = find_dependencies(declarations)
        problem_decl = None
        
        # Find the file/line associated with the actual Error (not Warnings)
        error_line_num = None
        all_file_matches = list(re.finditer(r'File "([^"]+)", line (\d+)', error_output))
        error_pos = error_output.find('\nError:')
        if error_pos == -1:
            error_pos = error_output.find('Error:')
        if error_pos >= 0 and all_file_matches:
            for match in reversed(all_file_matches):
                if match.start() <= error_pos:
                    error_line_num = int(match.group(2)) - 1
                    break
        if error_line_num is None and all_file_matches:
            error_line_num = int(all_file_matches[0].group(2)) - 1
        
        if error_line_num is not None:
            for decl in declarations:
                if decl['start_line'] <= error_line_num <= decl['end_line']:
                    problem_decl = decl
                    break
        
        if not problem_decl:
            print(f"    Cannot locate error in {file_path.name}:")
            for line in error_output.split('\n')[:3]:
                if line.strip():
                    print(f"      {line}")
            return False
        
        # If removable_names is set, refuse to remove anything outside that set
        if removable_names is not None and problem_decl['name'] not in removable_names:
            return False
        
        to_remove = compute_transitive_dependents(dependencies, {problem_decl['name']})
        
        # If constrained, only remove declarations within the removable set
        if removable_names is not None:
            to_remove = to_remove.intersection(removable_names)
        
        lines = read_file_lines(file_path)
        lines_to_remove = set()
        for decl in declarations:
            if decl['name'] in to_remove:
                for line_num in range(decl['start_line'], decl['end_line'] + 1):
                    lines_to_remove.add(line_num)
        
        filtered_lines = [line for i, line in enumerate(lines) if i not in lines_to_remove]
        write_file_lines(file_path, filtered_lines)
        
        # Update removable_names to reflect what was removed
        if removable_names is not None:
            removable_names -= to_remove
        
        print(f"    Removed: {', '.join(sorted(to_remove))}")
    
    return False

# =============================================================================
# Main Functions
# =============================================================================

def preprocess_ghost_vc_files(gvc_vc_file: Path, gvc_hl_file: Path, 
                               work_dir: Path, config_path: Path = None) -> tuple[bool, Path]:
    """
    Preprocess offline-lemma files: validate, fix, and merge.
    Returns (success, merged_file_path). Fails only if merged file cannot compile.
    """
    temp_gvc_vc = work_dir / f"temp_{gvc_vc_file.stem}.v"
    temp_gvc_hl = work_dir / f"temp_{gvc_hl_file.stem}.v"
    temp_merged = work_dir / "temp_merged_ghost_vc.v"
    
    shutil.copy2(gvc_vc_file, temp_gvc_vc)
    shutil.copy2(gvc_hl_file, temp_gvc_hl)
    
    # Fix each file (treat as empty if unfixable)
    print("  Fixing ghost_vc.v...")
    if not iteratively_fix_file(temp_gvc_vc, config_path):
        print("    -> treating as empty")
        write_file_content(temp_gvc_vc, "")
    
    print("  Fixing helper_lemmas.v...")
    if not iteratively_fix_file(temp_gvc_hl, config_path):
        print("    -> treating as empty")
        write_file_content(temp_gvc_hl, "")
    
    # Merge and fix
    print("  Merging and validating...")
    merged = read_file_content(temp_gvc_vc) + "\n\n" + read_file_content(temp_gvc_hl)
    write_file_content(temp_merged, merged)
    
    if not iteratively_fix_file(temp_merged, config_path):
        print("    -> merge failed")
        return False, None
    
    return True, temp_merged


def run_proof_search(idx: int, vc_file: Path, plan_file: Path, config: Path, compile_only: bool = False) -> int:
    """Run proof search on a single file. Returns exit code."""
    if compile_only:
        success, error = compile_coq_file(vc_file, config)
        if success:
            print(f"  Compilation successful!")
        else:
            print(f"  Compilation failed: {error}")
        return 0 if success else 1
    
    cmd = [sys.executable, "-m", "main", str(vc_file), "--config", str(config)]
    if plan_file and plan_file.exists():
        cmd.extend(["--plan", str(plan_file)])
    
    print(f"\n[{idx}] {vc_file.name}")
    result = subprocess.run(cmd, cwd=str(PROOF_SEARCH_DIR))
    return result.returncode


def get_latest_autorocq_dir(directory: Path) -> Path:
    """Find the latest autorocq-* directory in the given directory."""
    autorocq_dirs = sorted(directory.glob("autorocq-*"))
    return autorocq_dirs[-1] if autorocq_dirs else None


def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="Run proof search on benchmark files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Available benchmarks:
{chr(10).join(f"  - {name}" for name in BENCHMARK_CONFIGS.keys())}

Examples:
  python run.py --benchmark svcomp-ablation --compile-only
  python run.py --benchmark svcomp-ablation --skip-preprocessing

Note: By default, Ghost-VC files undergo preprocessing (validation, fixing, merging).
Use --skip-preprocessing to bypass all Ghost-VC processing and use only the original VC file.
        """
    )
    parser.add_argument(
        "--benchmark",
        type=str,
        required=True,
        choices=list(BENCHMARK_CONFIGS.keys()),
        help="Benchmark to run (sets root directories and benchmark file)"
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Maximum number of items to process from the benchmark file (default: all items)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help=f"Output directory for results (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "--skip-preprocessing",
        action="store_true",
        help="Skip all Ghost-VC processing and use only the original VC file"
    )
    parser.add_argument(
        "--compile-only",
        action="store_true",
        help="Only compile the pre-procesed file, do not run the proof search"
    )
    
    args = parser.parse_args()
    
    # Get benchmark configuration
    benchmark_config = BENCHMARK_CONFIGS[args.benchmark]
    BENCHMARK_ROOT = benchmark_config["root"]
    BENCHMARK_FILE = benchmark_config["file"]
    GVC_RESULT_DIR = benchmark_config["gvc_result_dir"]
    CONFIG_TYPE = benchmark_config.get("config_type", "shared")
    
    # Set output directory (use command line arg if provided, otherwise use default)
    OUTPUT_DIR = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
    
    print(f"Benchmark: {args.benchmark} ({CONFIG_TYPE} config)")
    if args.max_items:
        print(f"Max items: {args.max_items}")
    
    # Read the benchmark file
    if not BENCHMARK_FILE.exists():
        print(f"Error: Benchmark file not found: {BENCHMARK_FILE}")
        sys.exit(1)
    
    # Check if shared config file exists
    if CONFIG_TYPE == "shared" and not CONFIG_FILE.exists():
        print(f"Error: Config file not found: {CONFIG_FILE}")
        sys.exit(1)
    
    # Create output directory if it doesn't exist
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    # Create or clean work directory
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(BENCHMARK_FILE, 'r') as f:
        lines = f.readlines()
    
    # Filter out empty lines
    lines = [line.strip() for line in lines if line.strip()]
    
    # Apply max_items limit if specified
    if args.max_items:
        lines = lines[:args.max_items]
    print(f"Processing {len(lines)} items\n")

    
    # Statistics
    total = 0
    success = 0
    failed = 0
    skipped = 0
    
    for line in lines:
        total += 1
        
        # Construct full path to the original .v file
        original_vc_file = BENCHMARK_ROOT / line
        
        # Check if the file exists
        if not original_vc_file.exists():
            print(f"Warning: File not found, skipping: {original_vc_file}")
            skipped += 1
            continue
        
        if CONFIG_TYPE == "per_file":
            # Per-file config is in the same directory as the .v file
            config_path = original_vc_file.parent / "local.json"
            # Check if config exists
            if not config_path.exists():
                print(f"Warning: Config not found, skipping: {config_path}")
                skipped += 1
                continue
        else:
            config_path = CONFIG_FILE
        
        # Process file with unified logic
        file_stem = original_vc_file.stem  # filename without extension
        
        # Create a unique filename in work directory by including parent directory
        # e.g., strcat/strcat_assert_10.v -> strcat_strcat_assert_10.v
        relative_path = Path(line)
        parent_dir = relative_path.parent.name if relative_path.parent.name else "root"
        parent_dir_clean = parent_dir.replace("-", "_").replace(".", "_")
        work_filename = f"{parent_dir_clean}_{original_vc_file.name}"
        work_vc_file = WORK_DIR / work_filename
        shutil.copy2(original_vc_file, work_vc_file)
        
        # Ghost-VC preprocessing
        work_hl_file = WORK_DIR / HL_FILENAME
        work_plan_file = None
        
        if not args.skip_preprocessing:
            gvc_dir = GVC_RESULT_DIR / parent_dir / file_stem
            gvc_vc_file = gvc_dir / GVC_FILENAME
            gvc_hl_file = gvc_dir / HL_FILENAME
            gvc_plan_file = gvc_dir / PLAN_FILENAME
            
            if gvc_dir.exists() and gvc_vc_file.exists() and gvc_hl_file.exists():
                print(f"\nPreprocessing {file_stem}...")
                success_preprocess, merged = preprocess_ghost_vc_files(gvc_vc_file, gvc_hl_file, WORK_DIR, config_path)
                
                if success_preprocess:
                    shutil.copy2(merged, work_hl_file)
                    if gvc_plan_file.exists():
                        shutil.copy2(gvc_plan_file, WORK_DIR / PLAN_FILENAME)
                        work_plan_file = WORK_DIR / PLAN_FILENAME
                    print("  Augmenting VC file...")
                    merge_coq_file(work_vc_file, work_hl_file)
        
        # Run proof search with the config file on the copied file
        return_code = run_proof_search(total, work_vc_file, work_plan_file, config_path, args.compile_only)
        
        if return_code == 0:
            success += 1
        else:
            failed += 1
            
        if args.compile_only:
            continue
        
        # Determine the output directory for this file
        # e.g., hex2bin/hex2bin_assert.v -> out/hex2bin/hex2bin_assert/
        result_dir = OUTPUT_DIR / parent_dir / file_stem
        result_dir.mkdir(parents=True, exist_ok=True)
        
        # Move all files that start with the work filename prefix
        # e.g., strcat_strcat_assert_10.v, strcat_strcat_assert_10.json, etc.
        file_extensions = ['.v', '.json', '.png']
        for ext in file_extensions:
            pattern = f"*{ext}"
            for file in WORK_DIR.glob(pattern):
                # Restore original filename
                # e.g., strcat_strcat_assert_10.v -> strcat_assert_10.v
                original_name = file.name.replace(f"{parent_dir_clean}_", "", 1)
                dest_file = result_dir / original_name
                shutil.move(file, dest_file)
        
        # Move the latest autorocq-* directory to the result directory
        latest_autorocq = get_latest_autorocq_dir(WORK_DIR)
        if latest_autorocq:
            result_autorocq = result_dir / latest_autorocq.name
            shutil.move(str(latest_autorocq), str(result_autorocq))
        else:
            print(f"Warning: No autorocq directory found in {WORK_DIR}")
        
        for backup_file in WORK_DIR.glob(f"*.backup"):
            backup_file.unlink()
        for temp_file in WORK_DIR.glob(f"temp_*"):
            temp_file.unlink()
        for text_file in WORK_DIR.glob(f"*.txt"):
            text_file.unlink()
    
    # Print summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print('='*60)
    print(f"Total files: {total}")
    print(f"Successful:  {success}")
    print(f"Failed:      {failed}")
    print(f"Skipped:     {skipped}")


if __name__ == "__main__":
    main()
