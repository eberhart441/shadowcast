"""The node-core C tests read flattened copies of the golden wire fixtures; they must match the fixtures.

firmware/core/tests/vectors/{round_broadcast,gateway_record}.txt are written by scripts/gen_wire_vectors.py from
tests/fixtures/*.json. If this fails, a fixture changed without regenerating: run the script, then
firmware/core/test.sh.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gen_wire_vectors  # noqa: E402


@pytest.mark.parametrize("name", sorted(gen_wire_vectors.TARGETS))
def test_vector_file_matches_its_fixture(name):
    on_disk = (gen_wire_vectors.VECTORS / name).read_text()
    assert on_disk == gen_wire_vectors.render(name), f"regenerate with scripts/gen_wire_vectors.py ({name})"
