"""Shared human-readable stream/round label presentation (issue #208).

`bbbffl_round.label` is already operator-readable for `ordinary` and
`finals` rounds (an ordinary round's label is its fixture round name; a
finals round's is `app.finals.WEEK_LABELS`, e.g. "Finals Week 1"). A
`superscore` round's stored label is the terse internal key
`app.superscore_round.ROUND_LABELS` uses ("SS1".."SS4"). This is the one
place that expands it to the operator-facing wording issue #208's
acceptance criteria require ("SuperScore 1"), never a second copy of that
mapping, and never something the coach/delegated/Scorer surfaces derive
independently.

Zero internal dependencies -- a pure formatting leaf any layer can import.
"""

import re

_SUPERSCORE_ROUND_LABEL = re.compile(r"^SS([1-4])$")


def humanize_round_label(stream_type: str, round_label: str) -> str:
    """`round_label` as an operator should read it: unchanged for
    `ordinary`/`finals` (already human-readable), expanded for
    `superscore` ("SS1" -> "SuperScore 1")."""
    if stream_type == "superscore":
        match = _SUPERSCORE_ROUND_LABEL.match(round_label)
        if match:
            return f"SuperScore {match.group(1)}"
    return round_label
