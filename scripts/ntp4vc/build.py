#!/usr/bin/env python3
"""
Script to generate local.json config files for each lemma file in the test set.

For each .v file in the list, this script:
1. Reads the dune file in the same directory
2. Parses the theory dependencies
3. Builds required libraries with dune (if not already built)
4. Maps each dependency to the correct library path
5. Generates a local.json file with the appropriate library_paths
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Tuple, Set


# Base paths
NTP4VC_ROOT = Path(os.path.abspath("./benchmarks/ntp4vc"))
BUILD_DIR = NTP4VC_ROOT / "_build/default"
WHY3_LIB_PATH = BUILD_DIR / "generation/rocq/Why3"
DATA_WHY3_DIR = NTP4VC_ROOT / "data/why3"

# Track libraries that have been built in this session
_built_libraries: Set[str] = set()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is not set in the environment")

# Config template (everything except library_paths)
CONFIG_TEMPLATE = {
    "llm": {
        "model": "openai/gpt-5.2-2025-12-11",
        "temperature": 0.0,
        "max_tokens": 2000,
        "timeout": 30,
        "api_key": OPENAI_API_KEY,
        "enable_caching": True
    },
    "coq": {
        "timeout": 60,
        "max_steps": 100,
        "workspace": None,
        "library_paths": [],  # Will be filled in
        "auto_setup_coqproject": True,
        "coqproject_extra_options": []
    },
    "ablation": {
        "enable_recording": True,
        "enable_error_feedback": True,
        "enable_hammer": False,
        "enable_history_context": True,
        "enable_rollback": True,
        "enable_context_search": True,
        "enable_helper_lemma": True, 
        "max_context_search": 3,
        "max_errors": 3
    },
    "log_level": "INFO",
    "output_dir": None
}

# Global cache for library name -> source path mapping
_library_cache: Dict[str, Path] = {}


def build_library_cache():
    """
    Scan all dune files in data/why3 to build a mapping from library names to their source paths.
    This handles different directory structures:
    - Pattern A: <project>/lib/rocq/<name>/ (e.g., imp/lib/rocq/imp/)
    - Pattern B: <project_vcg>/rocq/<name>/ (e.g., pairing_heap_vcg/rocq/pairing_heap/)
    """
    global _library_cache
    
    if _library_cache:
        return  # Already built
    
    print("Building library cache...")
    
    # Find all dune files
    for dune_path in DATA_WHY3_DIR.rglob("dune"):
        try:
            with open(dune_path, 'r') as f:
                content = f.read()
            
            # Remove newlines and extra spaces for easier parsing
            content = ' '.join(content.split())
            
            # Extract theory name: (name <name>)
            name_match = re.search(r'\(name\s+([^\)]+)\)', content)
            if name_match:
                theory_name = name_match.group(1).strip()
                # Store the directory containing this dune file
                _library_cache[theory_name] = dune_path.parent
        except Exception as e:
            print(f"Warning: Could not parse {dune_path}: {e}")
    
    print(f"Found {len(_library_cache)} libraries")


def is_library_built(lib_name: str) -> bool:
    """
    Check if a library has been built by looking for .vo files in the build directory.
    
    Args:
        lib_name: The library name (e.g., "Why3", "pairing_heap_vcg.pairing_heap")
    
    Returns:
        True if the library appears to be built (has .vo files), False otherwise
    """
    # Why3 is always assumed to be built (it's a core dependency)
    if lib_name == "Why3" or lib_name.startswith("Why3."):
        build_path = WHY3_LIB_PATH
        if build_path.exists():
            vo_files = list(build_path.glob("*.vo"))
            return len(vo_files) > 0
        return False
    
    # Build library cache if not already done
    build_library_cache()
    
    # Look up the library in the cache
    if lib_name not in _library_cache:
        return False
    
    source_path = _library_cache[lib_name]
    relative_path = source_path.relative_to(NTP4VC_ROOT)
    build_path = BUILD_DIR / relative_path
    
    if not build_path.exists():
        return False
    
    # Check for .vo files
    vo_files = list(build_path.glob("*.vo"))
    return len(vo_files) > 0


def build_library(lib_name: str) -> bool:
    """
    Build a library using dune.
    
    Args:
        lib_name: The library name to build
    
    Returns:
        True if build succeeded, False otherwise
    """
    global _built_libraries
    
    # Skip if already built in this session
    if lib_name in _built_libraries:
        return True
    
    # Why3 libraries - build the Why3 target
    if lib_name == "Why3" or lib_name.startswith("Why3."):
        target = "generation/rocq/Why3"
    else:
        # Build library cache if not already done
        build_library_cache()
        
        if lib_name not in _library_cache:
            print(f"Warning: Unknown library {lib_name}, cannot build")
            return False
        
        source_path = _library_cache[lib_name]
        target = str(source_path.relative_to(NTP4VC_ROOT))
    
    print(f"Building library: {lib_name} (target: {target})")
    
    try:
        result = subprocess.run(
            ["dune", "build", target],
            cwd=str(NTP4VC_ROOT),
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout per library
        )
        
        if result.returncode == 0:
            _built_libraries.add(lib_name)
            print(f"  Successfully built {lib_name}")
            return True
        else:
            print(f"  Failed to build {lib_name}")
            print(f"  stderr: {result.stderr[:500]}" if result.stderr else "")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"  Timeout building {lib_name}")
        return False
    except Exception as e:
        print(f"  Error building {lib_name}: {e}")
        return False


def build_required_libraries(rocq_dir: Path) -> bool:
    """
    Build all required libraries for a rocq directory.
    Only builds libraries that haven't been built yet.
    
    Args:
        rocq_dir: Path to the rocq directory
    
    Returns:
        True if all libraries are available, False if any failed to build
    """
    dune_path = rocq_dir / "dune"
    if not dune_path.exists():
        return False
    
    # Parse dune file to get dependencies
    _, theories = parse_dune_file(dune_path)
    
    all_success = True
    
    # Check and build each dependency
    for theory in theories:
        # Normalize Why3.* to just check/build Why3
        if theory.startswith("Why3."):
            theory = "Why3"
        
        if is_library_built(theory):
            # Already built, skip
            continue
        
        # Need to build this library
        if not build_library(theory):
            all_success = False
            print(f"Warning: Failed to build dependency {theory} for {rocq_dir}")
    
    return all_success


def parse_dune_file(dune_path: Path) -> Tuple[str, List[str]]:
    """
    Parse a dune file and extract the theory name and dependencies.
    
    Returns:
        Tuple of (theory_name, list_of_dependency_names)
    """
    with open(dune_path, 'r') as f:
        content = f.read()
    
    # Remove newlines and extra spaces for easier parsing
    content = ' '.join(content.split())
    
    # Extract theory name: (name <name>)
    name_match = re.search(r'\(name\s+([^\)]+)\)', content)
    if not name_match:
        raise ValueError(f"Could not find theory name in {dune_path}")
    theory_name = name_match.group(1).strip()
    
    # Extract theories: (theories <theory1> <theory2> ...)
    theories_match = re.search(r'\(theories\s+([^\)]+)\)', content)
    if theories_match:
        theories_str = theories_match.group(1).strip()
        theories = theories_str.split()
    else:
        theories = []
    
    return theory_name, theories


def get_library_path(lib_name: str, source_dir: Path) -> Dict[str, str]:
    """
    Map a library name to its path.
    
    Args:
        lib_name: The library name (e.g., "Why3", "imp.imp", "contiki_list.Compound")
        source_dir: The source directory of the current vcg (to determine pearl vs frama_c)
    
    Returns:
        Dict with "path" and "name" keys
    """
    # Why3 and all Why3.* libraries map to the same base path
    if lib_name == "Why3" or lib_name.startswith("Why3."):
        return {
            "path": str(WHY3_LIB_PATH),
            "name": "Why3"
        }
    
    # Build library cache if not already done
    build_library_cache()
    
    # Look up the library in the cache
    if lib_name in _library_cache:
        source_path = _library_cache[lib_name]
        # Convert source path to build path
        # Source: /home/.../ntp4vc/data/why3/pearl/xxx/rocq/yyy
        # Build:  /home/.../ntp4vc/_build/default/data/why3/pearl/xxx/rocq/yyy
        relative_path = source_path.relative_to(NTP4VC_ROOT)
        build_path = BUILD_DIR / relative_path
        
        return {
            "path": str(build_path),
            "name": lib_name
        }
    
    # Fallback: try to construct path using old logic
    # For other libraries in format "<prefix>.<name>"
    parts = lib_name.split(".")
    if len(parts) < 2:
        raise ValueError(f"Unexpected library name format: {lib_name}")
    
    prefix = parts[0]  # e.g., "imp", "avl", "contiki_list", "multiprecision"
    name = parts[-1]   # e.g., "imp", "dict", "Compound"
    
    # Determine if this is pearl or frama_c based on source directory
    source_dir_str = str(source_dir)
    if "frama_c" in source_dir_str:
        base_category = "frama_c"
    else:
        base_category = "pearl"
    
    # Try Pattern A: <prefix>/lib/rocq/<name>
    lib_path_a = BUILD_DIR / "data/why3" / base_category / prefix / "lib/rocq" / name
    # Try Pattern B: <prefix>/rocq/<name>
    lib_path_b = BUILD_DIR / "data/why3" / base_category / prefix / "rocq" / name
    
    # Check which one exists (check source dirs since build might not exist yet)
    source_path_a = NTP4VC_ROOT / "data/why3" / base_category / prefix / "lib/rocq" / name
    source_path_b = NTP4VC_ROOT / "data/why3" / base_category / prefix / "rocq" / name
    
    if source_path_a.exists():
        lib_path = lib_path_a
    elif source_path_b.exists():
        lib_path = lib_path_b
    else:
        # Default to pattern A
        print(f"Warning: Could not find library {lib_name}, using default path")
        lib_path = lib_path_a
    
    return {
        "path": str(lib_path),
        "name": lib_name
    }


def generate_config_for_directory(rocq_dir: Path) -> Dict:
    """
    Generate the config for a given rocq directory.
    
    Args:
        rocq_dir: Path to the rocq directory containing the .v file and dune file
    
    Returns:
        The complete config dict
    """
    dune_path = rocq_dir / "dune"
    
    if not dune_path.exists():
        raise FileNotFoundError(f"Dune file not found: {dune_path}")
    
    # Parse dune file
    theory_name, theories = parse_dune_file(dune_path)
    
    # Build library_paths
    library_paths = []
    
    # Track which Why3 paths we've already added (to avoid duplicates)
    seen_libs = set()
    
    # First add Why3 if it's in the dependencies
    for theory in theories:
        if theory == "Why3" or theory.startswith("Why3."):
            if "Why3" not in seen_libs:
                library_paths.append({
                    "path": str(WHY3_LIB_PATH),
                    "name": "Why3"
                })
                seen_libs.add("Why3")
    
    # Then add other dependencies
    for theory in theories:
        if theory == "Why3" or theory.startswith("Why3."):
            continue  # Already handled
        
        if theory not in seen_libs:
            lib_entry = get_library_path(theory, rocq_dir)
            library_paths.append(lib_entry)
            seen_libs.add(theory)
    
    # Add the current theory itself (points to source directory)
    library_paths.append({
        "path": str(rocq_dir),
        "name": theory_name
    })
    
    # Create config from template
    config = json.loads(json.dumps(CONFIG_TEMPLATE))  # Deep copy
    config["coq"]["library_paths"] = library_paths
    
    return config


def generate_configs_for_ablation(list_file):
    
    with open(list_file, 'r') as f:
        lines = f.readlines()
    
    # Track directories we've processed (to avoid duplicates)
    processed_dirs = set()
    
    # Statistics
    success_count = 0
    skip_count = 0
    error_count = 0
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Get the full path to the .v file
        v_file_path = NTP4VC_ROOT / line
        rocq_dir = v_file_path.parent
        
        # Skip if we've already processed this directory
        if str(rocq_dir) in processed_dirs:
            print(f"Skipping (already processed): {rocq_dir}")
            skip_count += 1
            continue
        
        processed_dirs.add(str(rocq_dir))
        
        # Generate config
        try:
            # Build required libraries (if not already built)
            build_required_libraries(rocq_dir)
            
            config = generate_config_for_directory(rocq_dir)
            
            # Write to local.json
            output_path = rocq_dir / "local.json"
            with open(output_path, 'w') as f:
                json.dump(config, f, indent=2)
            
            print(f"Generated: {output_path}")
            success_count += 1
            
        except Exception as e:
            print(f"Error processing {rocq_dir}: {e}")
            error_count += 1
    
    print(f"\nSummary:")
    print(f"  Successfully generated: {success_count}")
    print(f"  Skipped (duplicates): {skip_count}")
    print(f"  Errors: {error_count}")


if __name__ == "__main__":
    print(f"NTP4VC_ROOT: {NTP4VC_ROOT}")
    generate_configs_for_ablation("benchmarks/ntp4vc-ablation.txt")
    generate_configs_for_ablation("benchmarks/ntp4vc-remaining.txt")
