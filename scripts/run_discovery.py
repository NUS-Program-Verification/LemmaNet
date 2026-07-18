#!/usr/bin/env python3
import re
import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Tuple, Optional, Dict


# Project paths
SCRIPT_DIR = Path(__file__).parent
AUTOROCQ_DIR = SCRIPT_DIR.parent
LEMMA_DIR = AUTOROCQ_DIR / "offline-lemma"
BENCHMARKS_DIR = AUTOROCQ_DIR / "benchmarks"
NTP4VC_SRC_BASE = AUTOROCQ_DIR / "source_programs" / "ntp4vc"
PROOF_SEARCH_DIR = AUTOROCQ_DIR / "proof-search"
SHARED_CONFIG_FILE = PROOF_SEARCH_DIR / "configs" / "local.json"

# Benchmark configurations
# config_type: "shared" uses a single shared config file for all files (svcomp)
#              "per_file" uses a per-file local.json in each .v file's parent dir (ntp4vc)
BENCHMARK_CONFIGS = {
    "svcomp-ablation": {
        "root": BENCHMARKS_DIR / "AutoRocq-bench" / "benchmarks" / "svcomp",
        "file": BENCHMARKS_DIR / "svcomp-ablation.txt",
        "source_base": None,
        "config_type": "shared",
    },
    "svcomp-remaining": {
        "root": BENCHMARKS_DIR / "AutoRocq-bench" / "benchmarks" / "svcomp",
        "file": BENCHMARKS_DIR / "svcomp-remaining.txt",
        "source_base": None,
        "config_type": "shared",
    },
    "ntp4vc-ablation": {
        "root": BENCHMARKS_DIR / "ntp4vc",
        "file": BENCHMARKS_DIR / "ntp4vc-ablation.txt",
        "source_base": NTP4VC_SRC_BASE,
        "config_type": "per_file",
    },
    "ntp4vc-remaining": {
        "root": BENCHMARKS_DIR / "ntp4vc",
        "file": BENCHMARKS_DIR / "ntp4vc-remaining.txt",
        "source_base": NTP4VC_SRC_BASE,
        "config_type": "per_file",
    },
}

# C type keywords for function signature detection
TYPE_KEYWORDS = ('void', 'int', 'bool', 'char', 'size_t', 'size_type', 'struct', 'static', 'inline', 'unsigned', 'u8')

# Regex pattern for matching function signatures
FUNC_SIG_PATTERN = r'\b(void|int|bool|char|size_t|size_type|struct|static|inline|unsigned|u8)\s+\*?\s*(\w+)\s*\([^)]*\)'

# C keywords that should not be matched as function names
C_CONTROL_KEYWORDS = frozenset(['if', 'while', 'for', 'switch', 'return', 'sizeof', 'void', 'int'])

# Loop contract keywords in ACSL
LOOP_CONTRACT_KEYWORDS = ('loop invariant', 'loop assigns', 'loop variant')

# Source subdirectories to search
SOURCE_SUBDIRS = ["src", "src/src", "src/include"]

# Property location marker format
PROPERTY_MARKER = "// >>> PROPERTY LOCATION (line {line}) <<<"

IGNORED_PROPERTY_LINES = (
        '//@ assert \\true;', '//@ assert true;', 
        '//@ assert \\false;', '//@ assert false;',
        '/*@ assert rte: signed_overflow:',
        '/*@ assert rte: unsigned_overflow:',
        '/*@ assert reachability:',
        '//@ assert rte: is_nan_or_infinite:',
    )

def clean_function_lines(func_lines: list, target_line_content: str) -> Tuple[list, int]:
    """
    Clean function lines by removing trivial assert true lines (except the target)
    and consecutive empty lines.
    
    Returns: (cleaned_lines, relative_idx) where relative_idx is the position of target_line_content
    """
    # Filter out trivial or safety properties for readability
    # Keep only if it's the target line
    filtered_lines = [
        line for line in func_lines 
        if not line.strip().startswith(IGNORED_PROPERTY_LINES) or line == target_line_content
    ]
    
    # Remove consecutive empty lines
    cleaned_lines = []
    for line in filtered_lines:
        if line.strip() == '' and cleaned_lines and cleaned_lines[-1].strip() == '':
            continue  # Skip consecutive empty line
        cleaned_lines.append(line)
    
    # Find target line position in cleaned list
    relative_idx = next((i for i, line in enumerate(cleaned_lines) if line == target_line_content), -1)
    
    return cleaned_lines, relative_idx


def clean_annotation_text(text: str) -> str:
    return text.lstrip('//').lstrip('/*').lstrip('@').lstrip()


def is_loop_contract_block(lines: list, start_idx: int) -> bool:
    """Check if a /*@ block starting at start_idx is a loop contract (not a function contract)."""
    for j in range(start_idx, min(start_idx + 20, len(lines))):
        line = lines[j]
        if any(kw in line for kw in LOOP_CONTRACT_KEYWORDS):
            return True
        if '*/' in line:
            break
    return False


def get_source_search_dirs(source_base: Path, project_name: str, include_other_projects: bool = False) -> list:
    """Get list of source directories to search."""
    search_dirs = []
    project_base = source_base / project_name
    
    for src_subdir in SOURCE_SUBDIRS:
        search_base = project_base / src_subdir
        if search_base.exists():
            search_dirs.append(search_base)
    
    if include_other_projects and source_base.exists():
        for other_project in source_base.iterdir():
            if other_project.is_dir() and other_project.name != project_name:
                for src_subdir in SOURCE_SUBDIRS:
                    search_base = other_project / src_subdir
                    if search_base.exists():
                        search_dirs.append(search_base)
    
    return search_dirs


def find_source_file_in_project(source_base: Path, project_name: str, source_file_meta: str) -> Optional[str]:
    """Search for a source file in a project's directories."""
    search_dirs = get_source_search_dirs(source_base, project_name)
    filename = Path(source_file_meta).name
    
    for search_base in search_dirs:
        # Try direct path first
        direct_path = search_base / source_file_meta
        if direct_path.exists():
            return str(direct_path)
        
        # Search recursively for the filename
        for found in search_base.rglob(filename):
            return str(found)
    
    return None


def find_metadata_source(v_file_path: Path) -> Tuple[Optional[Path], str]:
    """
    Find metadata source (report.json or .mlw file) for a .v file.
    
    Returns: (metadata_path, source_type) where source_type is 'report' or 'mlw'
    """
    # Try report.json first (svcomp benchmarks)
    report_path = v_file_path.parent / "report.json"
    if report_path.exists():
        return (report_path, 'report')
    
    # Try .mlw file (ntp4vc benchmarks)
    # V file path: .../contiki_list/array_pop_Why3_ide_vcg/rocq/array_pop_Why3_ide_VC...goal17.v
    # MLW file: .../contiki_list/array_pop_Why3_ide.mlw
    # Note: V files for functions starting with underscores have X prefix (e.g., X__fflush_Why3_ide_VC...)
    v_filename = v_file_path.stem
    match = re.match(r'(.+?)_Why3_ide_VC', v_filename)
    if match:
        func_part = match.group(1)
        # Strip X prefix if function name starts with underscore (e.g., X__fflush -> __fflush)
        if func_part.startswith('X_'):
            func_part = func_part[1:]  # Remove the X
        base_name = func_part + "_Why3_ide"
        # MLW is in parent.parent.parent
        mlw_path = v_file_path.parent.parent.parent / f"{base_name}.mlw"
        if mlw_path.exists():
            return (mlw_path, 'mlw')
    
    return (None, 'none')


def extract_metadata_from_report(report_json: Path, v_filename: str) -> Optional[Dict]:
    data = json.loads(report_json.read_text())
    base_name = v_filename.replace('.v', '')
    
    for entry in data:
        goal = entry.get('goal', '')
        if base_name in goal or goal.endswith(base_name):
            return {
                'file': entry.get('file'),
                'line': entry.get('line'),
                'function': entry.get('function'),
                'property': entry.get('property'),
                'goal': goal
            }
    return None


def extract_metadata_from_mlw(mlw_file: Path, v_filename: str, source_base: Optional[Path] = None) -> Optional[Dict]:
    content = mlw_file.read_text(errors='ignore')
    
    match = re.match(r'.+_Why3_ide_(VC.+?)_goal\d+\.v$', v_filename)
    if not match:
        return None
    
    theory_name = match.group(1)
    theory_match = re.search(rf'theory\s+{re.escape(theory_name)}\b', content)
    if not theory_match:
        return None
    
    text_before = content[:theory_match.start()]
    search_text = text_before[-500:] if len(text_before) > 500 else text_before
    
    # Find all (file ..., line ...) matches - prefer the last one (call site for function calls)
    file_line_matches = re.findall(r'\(file\s+([^,]+),\s*line\s+(\d+)\)', search_text)
    file_line_match = file_line_matches[-1] if file_line_matches else None
    
    # For call pre-conditions, we want the caller function (pattern: "in 'caller' at call")
    # Otherwise, use the first "in 'function'" match
    call_func_match = re.search(r"in\s+'(\w+)'\s+at\s+call", search_text)
    if call_func_match:
        func_name = call_func_match.group(1)
    else:
        func_match = re.search(r"in\s+'(\w+)'", search_text)
        if func_match:
            func_name = func_match.group(1)
        else:
            func_match = re.match(r'VC(.+?)_(loop|post|assert|call|assign|stmt|disjoint|complete)', theory_name)
            func_name = func_match.group(1) if func_match else "unknown"
    
    if file_line_match:
        source_file = file_line_match[0].strip()
        line_num = int(file_line_match[1])
    else:
        # Fallback: extract from comment and search in source files
        # Patterns:
        # 1(a) "Post-condition 'property' in 'function'"
        # 1(b) "Post-condition for 'behavior' 'property' in 'function'"
        comment_match = re.search(r"(Post-condition)(?:\s+for\s+'(\w+)')?\s+'(\w+)'\s+in\s+'(\w+)'", search_text)
        
        # 2. Pattern for "Disjoint behaviors" or "Complete behaviors"
        behaviors_match = re.search(r"(Disjoint|Complete)\s+behaviors\s+'(\w+)'", search_text)
        
        # 3. Pattern for "Post-condition 'name' at block" (stmt post-conditions without function in comment)
        block_post_match = re.search(r"Post-condition\s+'(\w+)'\s+at\s+block", search_text)
        
        # Extract project from mlw path
        project_name = extract_project_name_from_path(mlw_file)
        if not project_name or not source_base:
            return None
        
        result = None
        if comment_match:
            property_type = comment_match.group(1)
            behavior_name = comment_match.group(2)  # May be None
            property_name = comment_match.group(3)
            comment_func_name = comment_match.group(4)
            # Use function name from comment if available
            if comment_func_name and comment_func_name != 'unknown':
                func_name = comment_func_name
            result = search_property_in_sources(source_base, project_name, property_name, func_name, property_type)
        
        elif behaviors_match:
            # Handle "Disjoint behaviors" or "Complete behaviors" patterns
            result = search_function_behaviors_in_sources(source_base, project_name, func_name)
        
        elif block_post_match:
            # Handle "Post-condition 'name' at block" - search for the postcondition in function
            property_name = block_post_match.group(1)
            result = search_property_in_sources(source_base, project_name, property_name, func_name, 'Post-condition')
        
        if result:
            source_file = Path(result[0]).name
            line_num = result[1]
        else:
            return None
    
    return {
        'file': source_file,
        'line': line_num,
        'function': func_name,
        'property': theory_name,
        'goal': theory_name
    }


def extract_function_containing_line(source_file: str, target_line: int) -> Tuple[str, str, str]:
    lines = Path(source_file).read_text(errors='ignore').splitlines()
    
    if target_line < 1 or target_line > len(lines):
        print(f"Warning: Line {target_line} out of bounds (file has {len(lines)} lines)", file=sys.stderr)
        return ("", "unknown", f"line {target_line} (out of bounds)")
    
    # Get actual annotation text at target line
    annotation_text = lines[target_line - 1].strip()
    annotation_text = clean_annotation_text(annotation_text)
    
    # Find function containing this line (scan backwards)
    func_sig_line = None
    func_name = "unknown"
    func_end = None
    contract_start = None
    
    # Scan backwards to find function signature
    
    for i in range(target_line - 1, max(0, target_line - 500), -1):
        line = lines[i]
        stripped = line.strip()
        
        # Skip empty lines and C++ comments
        if not stripped or stripped.startswith('//'):
            continue
        
        # If we hit an ACSL comment start /*@ that spans multiple lines (contract block),
        # look forward to find the function signature
        # Skip single-line ACSL annotations like /*@ assert ... */
        if (stripped.startswith('/*@') or re.match(r'\s*/\*@', line)) and '*/' not in stripped:
            # Check if this is a loop contract (not a function contract) by looking at content
            # Loop contracts are INSIDE functions, so we should skip them and continue backward search
            if is_loop_contract_block(lines, i):
                continue  # Skip loop contracts, continue backward search for function
            
            # Multi-line ACSL function contract - search forward for the function signature (after closing */)
            # Use large range to handle very long contracts
            for j in range(i, min(i + 200, len(lines))):
                if '*/' not in lines[j]:
                    continue
                # Found end of ACSL comment, look for function signature after it
                for k in range(j + 1, min(j + 10, len(lines))):
                    if not any(kw in lines[k] for kw in TYPE_KEYWORDS):
                        continue
                    combined = ' '.join(lines[k:min(k+5, len(lines))])
                    match = re.search(FUNC_SIG_PATTERN, combined)
                    if match:
                        candidate = match.group(2)
                        if candidate not in C_CONTROL_KEYWORDS:
                            # Use this as the function, with contract starting at i
                            func_sig_line = k
                            func_name = candidate
                            contract_start = i
                            # Find function end
                            func_end = k
                            brace_count = 0
                            started = False
                            for m in range(k, min(len(lines), k + 1000)):
                                brace_count += lines[m].count('{') - lines[m].count('}')
                                if '{' in lines[m]:
                                    started = True
                                if started and brace_count == 0:
                                    func_end = m
                                    break
                            # Verify target is in range
                            if contract_start <= target_line - 1 <= func_end:
                                # Extract, clean, and return
                                func_lines = lines[contract_start:func_end + 1]
                                target_line_content = lines[target_line - 1]
                                func_lines, relative_idx = clean_function_lines(func_lines, target_line_content)
                                if 0 <= relative_idx < len(func_lines):
                                    target_indent = len(func_lines[relative_idx]) - len(func_lines[relative_idx].lstrip())
                                    func_lines.insert(relative_idx, f"{' ' * target_indent}{PROPERTY_MARKER.format(line=target_line)}")
                                return '\n'.join(func_lines), func_name, annotation_text
                    break
                break
            continue
        
        # Check if line contains a type keyword (potential function signature)
        if not any(kw in line for kw in TYPE_KEYWORDS):
            continue
        
        # Look for function signature pattern (may span multiple lines for params)
        combined = ' '.join(lines[i:min(i+10, len(lines))])
        
        # Match function definition: type name(params)
        match = re.search(FUNC_SIG_PATTERN, combined)
        
        if match:
            candidate = match.group(2)
            # Skip C keywords
            if candidate in C_CONTROL_KEYWORDS:
                continue
            
            # Verify the function name is on the current line or next few lines (not from much later)
            if candidate not in ' '.join(lines[i:min(i+3, len(lines))]):
                continue
            
            # Find the opening brace
            candidate_sig_line = None
            for j in range(i, min(i + 20, len(lines))):
                if '{' in lines[j]:
                    text_to_brace = ' '.join(lines[i:j+1])
                    if ')' in text_to_brace and text_to_brace.index(')') < text_to_brace.index('{'):
                        candidate_sig_line = i
                        break
            
            if candidate_sig_line is None:
                continue
            
            # Find function end to verify target_line is inside this function
            candidate_end = candidate_sig_line
            brace_count = 0
            started = False
            for k in range(candidate_sig_line, min(len(lines), candidate_sig_line + 1000)):
                brace_count += lines[k].count('{') - lines[k].count('}')
                if '{' in lines[k]:
                    started = True
                if started and brace_count == 0:
                    candidate_end = k
                    break
            
            # Find ACSL contract start for this candidate
            # Only look for function-level contracts, not loop contracts
            candidate_contract_start = candidate_sig_line
            for k in range(candidate_sig_line - 1, max(0, candidate_sig_line - 300), -1):
                line_k = lines[k]
                # Stop if we hit a closing brace (end of another function)
                if line_k.strip() == '}':
                    break
                if re.match(r'\s*/\*@', line_k):
                    # Check if this is a loop contract (skip it)
                    if not is_loop_contract_block(lines, k):
                        candidate_contract_start = k
                    continue
                if re.search(r'\b(void|int|char|struct)\s+\w+\s*\(', line_k) and k < candidate_sig_line - 10:
                    break
            
            # Check if target_line is within this function's range (including contract)
            # target_line is 1-indexed, contract_start is 0-indexed
            if candidate_contract_start <= target_line - 1 <= candidate_end:
                func_name = candidate
                func_sig_line = candidate_sig_line
                func_end = candidate_end
                contract_start = candidate_contract_start
                break
            # If target_line is not in this function, continue searching backwards
    
    if func_sig_line is None:
        return (annotation_text, "unknown", annotation_text)
    
    # Extract, clean, and add highlight
    func_lines = lines[contract_start:func_end + 1]
    target_line_content = lines[target_line - 1]
    func_lines, relative_idx = clean_function_lines(func_lines, target_line_content)
    
    if 0 <= relative_idx < len(func_lines):
        target_indent = len(func_lines[relative_idx]) - len(func_lines[relative_idx].lstrip())
        func_lines.insert(relative_idx, f"{' ' * target_indent}{PROPERTY_MARKER.format(line=target_line)}")
    
    return '\n'.join(func_lines), func_name, annotation_text


def extract_full_annotation(source_file: str, line_num: int, line_content: str) -> str:
    """Extract the full multi-line annotation body starting from a given line."""
    try:
        content = Path(source_file).read_text(errors='ignore')
        lines = content.splitlines()
        
        if line_num < 1 or line_num > len(lines):
            return line_content
        
        # Find the character position of the line start
        line_start = sum(len(lines[i]) + 1 for i in range(line_num - 1))
        return extract_annotation_body(content, line_start)
    except:
        return line_content

PROPERTY_KEYWORDS = ('requires', 'ensures', 'assigns', 'behavior', 
                     'loop invariant', 'loop assigns', 'loop variant',
                     'assert', 'disjoint', 'complete')

def extract_annotation_body(content: str, start_pos: int) -> str:
    """Extract multi-line annotation body starting from a position in content."""
    rest = content[start_pos:]
    lines = rest.split('\n')
    first_line = lines[0].strip()
    
    # Check if first line contains closing ACSL comment
    if '*/' in first_line:
        annotation_part = first_line[:first_line.index('*/')].rstrip('@').strip()
        return annotation_part
    
    # If first line is a complete annotation (ends with ;), return it immediately
    # But only if the first word after annotation keyword is not just ":" (incomplete)
    if first_line.rstrip().endswith(';') and not first_line.rstrip().endswith(':'):
        return first_line
    
    result_lines = [first_line]
    
    # C keywords that indicate we've left the annotation
    c_keywords = {'return', 'if', 'else', 'for', 'while', 'switch', 'case', 'break', 
                  'continue', 'goto', 'int', 'char', 'void', 'unsigned', 'signed', 
                  'struct', 'union', 'enum', 'typedef', 'static', 'const', 'volatile',
                  'extern', 'register', 'auto', 'sizeof', 'do'}
    
    for line in lines[1:50]:  # Limit to 50 lines
        stripped = line.strip()
        
        # Stop at next annotation keyword
        if stripped.startswith(PROPERTY_KEYWORDS):
            break
        # Stop at closing ACSL comment
        if '*/' in stripped:
            before_close = stripped[:stripped.index('*/')].rstrip('@').strip()
            if before_close:
                result_lines.append(before_close)
            break
        # Stop at empty line
        if not stripped:
            break
        
        # Check if this line looks like C code (not annotation continuation)
        first_word = stripped.split()[0] if stripped else ''
        # Remove trailing punctuation for keyword check
        first_word_clean = first_word.rstrip('({;:')
        is_c_code = first_word_clean in c_keywords or first_word in ('{', '}')
        
        if is_c_code:
            # If we already found a semicolon, we're done
            # If not, this is unexpected C code in the middle of annotation - also stop
            break
        
        # Add the line as continuation
        result_lines.append(stripped)
        if stripped.endswith(';'):
            break  # Complete annotation found
    
    return '\n'.join(result_lines)


def extract_project_name_from_path(path: Path) -> Optional[str]:
    """Extract project name from a path containing 'frama_c' directory."""
    parts = path.parts
    return next((parts[i+1] for i, p in enumerate(parts) if p == 'frama_c' and i+1 < len(parts)), None)


def search_property_in_sources(source_base: Path, project_name: str, property_name: str, 
                              func_name: str, property_type: str) -> Optional[Tuple[str, int]]:
    if property_type == 'Post-condition':
        search_pattern = rf'ensures\s+{re.escape(property_name)}\s*:'
    else:
        return None
    
    # Search in files that likely contain the function
    for search_base in get_source_search_dirs(source_base, project_name):
        for source_file in search_base.rglob('*.[ch]'):
            try:
                content = source_file.read_text(errors='ignore')
                
                # Check if this file contains the function
                if not re.search(rf'\b{func_name}\s*\(', content):
                    continue
                
                # Now search for the property in this file
                lines = content.splitlines()
                for i, line in enumerate(lines, 1):
                    if re.search(search_pattern, line):
                        return (str(source_file), i)
            except:
                continue
    return None


def search_function_behaviors_in_sources(source_base: Path, project_name: str, func_name: str) -> Optional[Tuple[str, int]]:
    """Search for function definition and return line where behaviors start."""
    func_pattern = rf'\b{func_name}\s*\('
    
    for search_base in get_source_search_dirs(source_base, project_name):
        for source_file in search_base.rglob('*.[ch]'):
            try:
                content = source_file.read_text(errors='ignore')
                lines = content.splitlines()
                
                # Find function definition
                for i, line in enumerate(lines):
                    if re.search(func_pattern, line):
                        # Search backwards for first behavior or requires clause
                        for j in range(i, max(0, i - 50), -1):
                            if 'behavior' in lines[j] or 'requires' in lines[j] or 'ensures' in lines[j]:
                                return (str(source_file), j + 1)
                        # Return function line if no contract found
                        return (str(source_file), i + 1)
            except:
                continue
    return None


def search_precondition_in_callee(source_base: Path, project_name: str, callee_func: str, precond_name: str) -> Optional[str]:
    """Search for a specific pre-condition in the callee function's source."""
    
    # Handle numeric suffixes (e.g., Separation_2 -> Separation, occurrence 2)
    suffix_match = re.match(r'(.+?)_(\d+)$', precond_name)
    base_precond_name = suffix_match.group(1) if suffix_match else precond_name
    occurrence = int(suffix_match.group(2)) if suffix_match else 1
    
    def extract_precond_from_file(source_file: Path, named_only: bool = True) -> Optional[str]:
        try:
            content = source_file.read_text(errors='ignore')
            
            func_match = re.search(rf'\b{callee_func}\s*\([^)]*\)\s*[;{{]', content)
            if not func_match:
                return None
            
            # Try named pre-condition first
            pattern = rf'requires\s+{re.escape(base_precond_name)}\s*:'
            matches = list(re.finditer(pattern, content))
            
            if matches:
                match = matches[min(occurrence - 1, len(matches) - 1)]
                return extract_annotation_body(content, match.start())
            
            # Fallback: extract all requires from contract
            if not named_only:
                func_pos = func_match.start()
                contract_pattern = r'/\*@(.*?)@\*/\s*(?:__\w+\s+)*(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:\w+\s+\*?\s*)+' + re.escape(callee_func)
                contract_match = re.search(contract_pattern, content[:func_pos + 100], re.DOTALL)
                if contract_match:
                    requires_clauses = re.findall(r'requires[^;]+;', contract_match.group(1))
                    if requires_clauses:
                        return '\n'.join(requires_clauses)
            
            return None
        except:
            return None
    
    search_dirs = get_source_search_dirs(source_base, project_name, include_other_projects=True)
    
    # First pass: named pre-conditions
    for search_base in search_dirs:
        for source_file in search_base.rglob('*.[ch]'):
            result = extract_precond_from_file(source_file, named_only=True)
            if result:
                return result
    
    # Second pass: any contract
    for search_base in search_dirs:
        for source_file in search_base.rglob('*.[ch]'):
            result = extract_precond_from_file(source_file, named_only=False)
            if result:
                return result
    
    return None


def reconstruct_property(base_name: str, line_content: str, source_base: Optional[Path], 
                         v_file: str, source_file: str, line_num: int = 0) -> str:
    """
    Reconstruct the full body of the property to prove based on VC type.
    This is used because frama-C may inject additional checks beyond the ACSL annotations. 
    For example, it may add checks for precondition, overflow, memory access, etc.
    """
    
    # If line_content is already an ACSL annotation, extract the full body
    if line_content.startswith(PROPERTY_KEYWORDS):
        full_annotation = extract_full_annotation(source_file, line_num, line_content)
        # Clean up any remaining ACSL comment markers
        if '*/' in full_annotation:
            full_annotation = full_annotation[:full_annotation.index('*/')].rstrip('@').strip()
        return clean_annotation_text(full_annotation)
    
    # Extract info from base_name (theory name)
    # Pattern: VC<func>_call_<callee>_pre_<precond>_part*
    call_pre_match = re.search(r'call_(\w+)_pre_(\w+?)(?:_part\d+)?(?:_goal\d+)?$', base_name)
    
    # Check for RTE overflow patterns
    overflow_match = re.search(r'assert_rte_(signed|unsigned)_overflow', base_name)
    
    # Check for RTE memory access patterns
    mem_access_match = re.search(r'assert_rte_mem_access', base_name)
    
    # Case 1: Pre-condition of callee function
    if call_pre_match:
        callee_func = call_pre_match.group(1)
        precond_name = call_pre_match.group(2)
        
        # Try to find the actual pre-condition from the callee's source
        if source_base:
            project_name = extract_project_name_from_path(Path(v_file))
            if project_name:
                precond = search_precondition_in_callee(source_base, project_name, callee_func, precond_name)
                if precond:
                    return f"/* call to {callee_func}() */\n{precond}"
        
        # Fallback: show the call site with pre-condition name
        return f"/* Pre-condition '{precond_name}' for call to {callee_func}() */\n\\requires {precond_name}: /* see {callee_func} definition */\n/* Call site: {line_content} */"
    
    # Case 2: Signed/Unsigned overflow RTE assertion
    if overflow_match:
        overflow_type = overflow_match.group(1)  # 'signed' or 'unsigned'
        boundary_type = 'INT' if overflow_type == 'signed' else 'UINT'
        
        # Pre/post increment/decrement: ++var, var++, --var, var--
        inc_match = re.search(r'\+\+(\w+)|(\w+)\+\+', line_content)
        dec_match = re.search(r'--(\w+)|(\w+)--', line_content)
        
        # Assignment with arithmetic: var = expr op val, var op= val
        assign_op_match = re.search(r'(\w+)\s*([+\-*/])=\s*(.+)', line_content)
        assign_expr_match = re.search(r'(\w+)\s*=\s*(.+?)\s*([+\-*/])\s*(.+)', line_content)
        
        if inc_match:
            var = inc_match.group(1) or inc_match.group(2)
            
            return f"assert rte: {overflow_type}_overflow: {var} <= {boundary_type}_MAX - 1;  /* no overflow on ++{var} */"
                    
        elif dec_match:
            var = dec_match.group(1) or dec_match.group(2)
            return f"assert rte: {overflow_type}_overflow: {var} >= {boundary_type}_MIN + 1;  /* no underflow on --{var} */"
        
        elif assign_op_match:
            var = assign_op_match.group(1)
            op = assign_op_match.group(2)
            val = assign_op_match.group(3).strip().rstrip(';')
            return f"assert rte: {overflow_type}_overflow: {boundary_type}_MIN <= {var} {op} ({val}) <= {boundary_type}_MAX;  /* no overflow on {var} {op} {val} */"
        
        elif assign_expr_match:
            var = assign_expr_match.group(1)
            lhs = assign_expr_match.group(2).strip()
            op = assign_expr_match.group(3)
            rhs = assign_expr_match.group(4).strip().rstrip(';')
            return f"assert rte: {overflow_type}_overflow: {boundary_type}_MIN <= ({lhs}) {op} ({rhs}) <= {boundary_type}_MAX;  /* no overflow on {var} = {lhs} {op} {rhs} */"
        
        # Fallback: generic overflow check
        clean_line = line_content.rstrip(';').strip()
        return f"assert /* no {overflow_type} overflow */ INT_MIN <= ({clean_line}) <= INT_MAX;"
    
    # Case 3: Memory access RTE assertion
    if mem_access_match:
        # Patterns for memory access
        patterns = [
            # *ptr dereference
            (r'\*\(([^)]+)\)', lambda m: f"\\valid({m.group(1)})"),
            (r'\*(\w+)', lambda m: f"\\valid({m.group(1)})"),
            # array[index] access
            (r'(\w+)\[([^\]]+)\]', lambda m: f"\\valid({m.group(1)} + ({m.group(2)}))"),
            # ptr->field access
            (r'(\w+)->(\w+)', lambda m: f"\\valid({m.group(1)})"),
            # (*ptr).field access
            (r'\(\*(\w+)\)\.(\w+)', lambda m: f"\\valid({m.group(1)})"),
        ]
        
        for pattern, formatter in patterns:
            match = re.search(pattern, line_content)
            if match:
                return f"assert rte: mem_access: {formatter(match)};  /* valid memory access in: {line_content.strip()} */"
        
        # Fallback: generic memory access check
        return f"assert rte: mem_access: \\valid(/* memory access */);  /* {line_content.strip()} */"
    
    return line_content



def process_proof(
    v_file: str, benchmarks_dir: str, source_base: Optional[Path], config_type: str,
    output_dir: str, api_key: str, model: str) -> bool:
    
    """Process one proof from .v file using metadata from report.json or .mlw file."""
    
    v_path = Path(benchmarks_dir) / v_file
    
    if not v_path.exists():
        print(f"Error: File not found: {v_path}")
        return False
    
    # Find metadata source (report.json or .mlw)
    metadata_path, source_type = find_metadata_source(v_path)
    if not metadata_path:
        print(f"Error: No metadata source (report.json or .mlw) found for {v_file}")
        return False
    
    metadata = extract_metadata_from_mlw(metadata_path, v_path.name, source_base) if source_type == 'mlw' else extract_metadata_from_report(metadata_path, v_path.name)
    
    if not metadata or not metadata['file'] or not metadata['line']:
        print(f"Error: Could not extract metadata from {source_type} for {v_file}")
        return False
    
    # Resolve source file path
    source_file_meta = metadata['file']
    source_file = None
    
    if os.path.isabs(source_file_meta) and os.path.exists(source_file_meta):
        # Absolute path that exists - use as is
        source_file = source_file_meta
    elif source_file_meta.startswith('source_programs/'):
        # Relative path (report.json)
        source_file = str(AUTOROCQ_DIR / source_file_meta)
    
    # If source_file not found yet, search in source_base
    if not source_file or not os.path.exists(source_file):
        if not source_base:
            return False
        
        # Extract project name from v_file path
        # Entry: data/why3/frama_c/contiki_list/array_pop_Why3_ide_vcg/rocq/...
        project_name = extract_project_name_from_path(Path(v_file))
        if not project_name:
            print(f"Error: Could not extract project name from {v_file}")
            return False
        
        # Search for source file in project directories
        source_file = find_source_file_in_project(source_base, project_name, source_file_meta)
        if not source_file:
            print(f"Error: Could not find {source_file_meta} in {source_base / project_name}")
            return False
    
    line_num = metadata['line']
    func_name = metadata['function'] or "unknown"
    
    # Extract function
    function_code, extracted_func, line_content = extract_function_containing_line(source_file, line_num)
    
    # sanity check 1: function code exists
    if not function_code or len(function_code) < 30:
        print(f"Error: Could not extract function from {source_file}:{line_num}")
        return False
    
    # sanity check 2: function name is correct
    if func_name != extracted_func and "unknown" not in [func_name, extracted_func] and "__VERIFIER_assert" not in [func_name, extracted_func]: 
        print(f"Error: Function name mismatch: {func_name} != {extracted_func}")
        return False
    
    base_name = v_path.stem
    parent_name = v_path.parent.name
    proof_dir = Path(output_dir) / parent_name / base_name
    proof_dir.mkdir(parents=True, exist_ok=True)
    
    # Reconstruct the property to prove based on the type of verification condition
    prop = reconstruct_property(base_name, line_content, source_base, v_file, source_file, line_num)
    
    property_details = f"""
Property name: {v_path.name}
Location: {Path(source_file).name}:line {line_num}
Function: {func_name}()
Property to prove:\n{prop}"""
    
    print(f"\n{'='*80}\n{property_details}\n{'-'*80}\n{function_code[:300]}...\n{'='*80}")
    
    # Determine library config path based on benchmark type
    if config_type == "per_file":
        # ntp4vc: per-file config is in the same directory as the .v file
        config_path = str(v_path.parent / "local.json")
    else:
        # svcomp: shared config file
        config_path = str(SHARED_CONFIG_FILE)
    
    discovery_cmd = "lemma_discovery.py"
    
    cmd = [
        # 'echo', # for debugging
        'python3', str(LEMMA_DIR / discovery_cmd),
        '--source', '/dev/stdin',
        '--wp-goal', str(v_path),
        '--base-name', base_name,
        '--output-dir', str(proof_dir),
        '--api-key', api_key,
        '--model', model,
        '--config', config_path,
    ]
    
    try:
        result = subprocess.run(cmd, 
                                input=property_details+'\n\nSource code with ACSL annotations:\n'+function_code, 
                                capture_output=False, text=True, timeout=600)
        return result.returncode == 0
    except:
        return False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Batch Coq proof validation with GPT",
        epilog=f"Available: {', '.join(BENCHMARK_CONFIGS.keys())}"
    )
    parser.add_argument('--benchmark', choices=list(BENCHMARK_CONFIGS.keys()),
                       help='Benchmark name')
    parser.add_argument('--output-dir', default='./out', help='Output directory')
    parser.add_argument('--api-key', help='OpenAI API key')
    parser.add_argument('--model', default='gpt-5.2', help='Model to use')
    parser.add_argument('--max-items', type=int, help='Max items to process')
    parser.add_argument('--skip', type=int, default=0, help='Skip first N items')
    args = parser.parse_args()
    
    # Get benchmark configuration
    config = BENCHMARK_CONFIGS[args.benchmark]
    benchmarks_dir = str(config['root'])
    source_base = config.get('source_base')
    lemma_list_file = config['file']
    config_type = config.get('config_type', 'shared')
    
    api_key = args.api_key or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        sys.exit("Error: No API key. Set OPENAI_API_KEY or use --api-key")
    
    print(f"Benchmark: {args.benchmark}")
    print(f"Model: {args.model}")
    
    # Read lemma list
    lemma_files = []
    for line in lemma_list_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            lemma_files.append(line)
    
    print(f"Total lemmas: {len(lemma_files)}")
    
    if args.skip:
        lemma_files = lemma_files[args.skip:]
    if args.max_items:
        lemma_files = lemma_files[:args.max_items]
    
    print(f"Processing: {len(lemma_files)} proofs\n")
    
    # Process each
    success = 0
    for i, v_file in enumerate(lemma_files, 1):
        print(f"\n[{i}/{len(lemma_files)}]")
        if process_proof(v_file, benchmarks_dir, source_base, config_type,
                         args.output_dir, api_key, args.model):
            success += 1
    
    # Summary
    print(f"\n{'='*80}")
    print(f"SUMMARY: {success}/{len(lemma_files)} successful")
    print(f"{'='*80}")


if __name__ == '__main__':
    main()
