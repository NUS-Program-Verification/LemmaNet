"""CoqPyt vendoring provenance tests."""

import tomllib
from pathlib import Path

import coqpyt


def test_vendored_coqpyt_is_pinned_to_upstream_1_1():
    metadata = tomllib.loads(
        (Path(coqpyt.__file__).parent / "UPSTREAM.toml").read_text(
            encoding="utf-8"
        )
    )

    assert coqpyt.__upstream_version__ == "1.1.0"
    assert metadata["upstream"]["tag"] == "v1.1.0"
    assert metadata["upstream"]["commit"] == (
        "f47237a3cdb8d0d9d6d3195971d529f5750fdf02"
    )
