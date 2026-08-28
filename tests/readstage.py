"""Parser for the playbook_edit read-stage framed format (012 phase 2).

The read stage returns a JSON header line followed by plain-text frames:

    {...header...}
    --- manifest ---
    ...
    --- code (candidate v3) ---
    ...
    --- language reference ---
    ...
    --- end ---

Each marker owns its leading newline, so sections round-trip exactly.
Returns a dict shaped like the old all-JSON payload (header keys plus
"manifest", "code", "code_label", "language_reference") so existing
tests keep their assertions.
"""

from __future__ import annotations

import json


def parse_read_stage(text: str) -> dict:
    header_line, sep, rest = text.partition("\n--- manifest ---\n")
    assert sep, f"not a framed read stage: {text[:200]!r}"
    out = json.loads(header_line)
    assert out.get("stage") == "read"
    manifest, sep, rest = rest.partition("\n--- code (")
    assert sep, "missing code frame"
    label, sep, rest = rest.partition(") ---\n")
    assert sep, "unterminated code label"
    code, sep, rest = rest.partition("\n--- language reference ---\n")
    assert sep, "missing language reference frame"
    ref, sep, _ = rest.partition("\n--- end ---")
    assert sep, "missing end frame"
    out["manifest"] = "" if manifest == "(none)" else manifest
    out["code"] = code
    out["code_label"] = label
    out["language_reference"] = ref
    return out
