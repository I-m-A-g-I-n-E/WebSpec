# gateway/tests/test_ufo_headers.py
"""Test that UFO headers are extracted and would be passed through."""
from webspec.guard import compute_clearance_token, build_provenance_link

def test_clearance_header_format(session_key):
    """Clearance header is token:timestamp format."""
    import time
    ts = str(int(time.time()))
    token = compute_clearance_token(session_key, "read", {"reference": "op://V/I/f"}, ts)
    header = f"{token}:{ts}"
    assert ":" in header
    parts = header.split(":")
    assert len(parts) == 2
    assert len(parts[0]) == 8

def test_provenance_header_format(session_key):
    """Provenance header is role:sig->role:sig format."""
    link = build_provenance_link(session_key, "human", "gateway", "read")
    header = f"human:{link}"
    assert ":" in header
    parts = header.split(":")
    assert parts[0] == "human"
    assert len(parts[1]) == 8
