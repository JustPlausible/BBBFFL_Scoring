"""Regression coverage for `app.draft_board`'s shared, `draft_kind`-aware
presentation layer (issue #181).

Codex review on PR #225 (P1) found that `draft_board_readiness` always read
the *preseason* draft's status regardless of the `draft_kind` its caller
actually wanted -- in the normal mid-season flow the preseason draft
already exists and is finalized, so `build_readiness(..., draft_kind=
"midseason")` would silently report readiness computed from the wrong
(finalized, unpaused) preseason draft instead of the real mid-season one.
These tests exercise `draft_board_readiness` directly against lightweight
fakes so the `draft_kind` propagation is pinned down without needing a
full two-draft season fixture.
"""

from dataclasses import dataclass

from app.draft_board import draft_board_readiness


@dataclass
class FakeEntry:
    season_entry_id: str


@dataclass
class FakeStatus:
    total_picks: int
    completed_picks: int
    target_squad_size: int
    is_paused: bool
    is_finalized: bool


class FakeIdentities:
    def __init__(self, entries):
        self._entries = entries

    def list_entries(self, season_id):
        return self._entries


class FakePlayerPool:
    def list_available(self, season_id):
        return []


class FakeDatabase:
    def execute(self, statement, parameters=()):
        class Row(dict):
            def __getitem__(self, key):
                return dict.get(self, key)

        return _One(Row(squad_limit=4))


class _One:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeDraftRepository:
    """Records which `draft_kind` each call received -- a real
    `app.draft.DraftRepository` keys every one of these off that kwarg."""

    def __init__(self, *, preseason_finalized, midseason_finalized):
        self.status_calls = []
        self.order_calls = []
        self.next_pick_calls = []
        self._finalized_by_kind = {"preseason": preseason_finalized, "midseason": midseason_finalized}

    def status(self, season_id, *, draft_kind="preseason"):
        self.status_calls.append(draft_kind)
        return FakeStatus(
            total_picks=10,
            completed_picks=10 if self._finalized_by_kind[draft_kind] else 3,
            target_squad_size=4,
            is_paused=False,
            is_finalized=self._finalized_by_kind[draft_kind],
        )

    def order(self, season_id, *, draft_kind="preseason"):
        self.order_calls.append(draft_kind)
        return [(i, f"entry-{i}") for i in range(10)]

    def next_pick(self, season_id, *, draft_kind="preseason"):
        self.next_pick_calls.append(draft_kind)
        return None


def test_readiness_reads_the_requested_draft_kinds_own_status_not_preseasons():
    """The exact Codex-flagged scenario: the preseason draft is already
    finalized, but a mid-season caller must see the mid-season draft's own
    (still in-progress) status, not the finalized preseason one."""
    entries = [FakeEntry(f"entry-{i}") for i in range(10)]
    draft = FakeDraftRepository(preseason_finalized=True, midseason_finalized=False)

    midseason_result = draft_board_readiness(
        FakeDatabase(), FakeIdentities(entries), draft, FakePlayerPool(), "season-1", draft_kind="midseason"
    )
    assert draft.status_calls[-1] == "midseason"
    assert draft.order_calls[-1] == "midseason"
    assert midseason_result["checks"]["draft_not_finalized"] is True

    preseason_result = draft_board_readiness(
        FakeDatabase(), FakeIdentities(entries), draft, FakePlayerPool(), "season-1", draft_kind="preseason"
    )
    assert draft.status_calls[-1] == "preseason"
    assert preseason_result["checks"]["draft_not_finalized"] is False


def test_readiness_defaults_to_preseason_for_backward_compatibility():
    entries = [FakeEntry(f"entry-{i}") for i in range(10)]
    draft = FakeDraftRepository(preseason_finalized=False, midseason_finalized=True)
    draft_board_readiness(FakeDatabase(), FakeIdentities(entries), draft, FakePlayerPool(), "season-1")
    assert draft.status_calls == ["preseason"]
