"""Replay captured E2E runs through the live timeline endpoints.

These tests load sanitized snapshots of real ``/api/e2e-run-detail/{id}``
inputs (E2E run records + worktree agent events + e2e.db row) and assert
the response matches a captured ``expected.json`` of test →
``issue_affordances`` mappings.

The point is to catch bugs that synthetic unit tests miss because the
test author cannot anticipate every realistic event shape — for example,
the view-filter regression that silently dropped issues whose only
in-window events were debug-tagged.

Adding a new fixture = adding new coverage. To capture a fixture from
a real run::

    .venv/bin/python scripts/snapshot_e2e_run.py --run-id 87 \
        --repo-root /path/to/checkout

If a captured fixture begins to fail because the contract LEGITIMATELY
changed (matcher logic, view-model shape), re-run the snapshot script
against the same run to bless the new expectations.
"""

from __future__ import annotations

import base64
import json
import shutil
import warnings
from functools import lru_cache
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="jsonschema.RefResolver is deprecated",
)

from jsonschema import Draft202012Validator, RefResolver
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from issue_orchestrator.contracts.ui_openapi_models import (
    E2ERunDetailPayload,
    E2ERunTimelinePayload,
    IssueDetailPayload,
)
from issue_orchestrator.entrypoints.control_api import (
    set_orchestrator as set_control_orchestrator,
)
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
from tests.fixtures.timeline_run_artifacts import (
    TimelineFixturePathField,
    rewrite_timeline_fixture_path,
    write_available_timeline_run_manifest,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "e2e_runs"
FIXTURE_REVIEW_PHASE_RECORDING_ROLES = {
    "review_exchange.round_started": "reviewer",
    "review_exchange.round_completed": "reviewer",
    "review.rework_started": "coder",
    "review.rework_completed": "coder",
}


def _discover_fixtures() -> list[Path]:
    """Find all fixture directories under tests/fixtures/e2e_runs/."""
    if not FIXTURE_DIR.exists():
        return []
    return sorted(
        d
        for d in FIXTURE_DIR.iterdir()
        if d.is_dir() and (d / "expected.json").exists()
    )


def _stage_fixture_layout(fixture: Path, tmp_path: Path) -> Path:
    """Copy a fixture into a tmp_path layout that the live endpoint accepts.

    The endpoint resolves the worktree timeline via
    ``get_e2e_worktree_path(repo_root)`` which expects a sibling directory
    named ``<repo_name>-e2e-worktree``. We honour that contract here so the
    real production code path runs unmodified against the fixture data.
    """
    repo_root = tmp_path / "repo"
    (repo_root / ".issue-orchestrator" / "state").mkdir(parents=True)
    wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
    wt_state.mkdir(parents=True)

    shutil.copy(fixture / "e2e.db", repo_root / ".issue-orchestrator" / "e2e.db")
    shutil.copy(
        fixture / "base_timeline.sqlite",
        repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )
    shutil.copy(
        fixture / "worktree_timeline.sqlite",
        wt_state / "timeline.sqlite",
    )
    _materialize_fixture_run_dirs(wt_state / "timeline.sqlite", tmp_path / "run-dirs")
    return repo_root


def _materialize_fixture_run_dirs(worktree_db: Path, run_dir_root: Path) -> None:
    """Replace sanitized fixture run_dir paths with real directories.

    Captured fixtures store machine-specific run paths as ``<TMP>/...``.
    The production action decorator correctly treats missing run directories
    as a malformed run-scoped event, so integration fixtures must satisfy that
    contract instead of accepting warning noise.
    """
    import sqlite3

    rows: list[tuple[int, int, str, str, str, str]] = []
    with sqlite3.connect(str(worktree_db)) as conn:
        for (
            sequence,
            issue_number,
            event_name,
            source_event,
            run_dir,
            data_json,
        ) in conn.execute(
            """
            SELECT sequence, issue_number, event, source_event, run_dir, data_json
            FROM timeline_events
            WHERE run_dir != ''
            ORDER BY sequence ASC
            """
        ):
            rows.append(
                (
                    int(sequence),
                    int(issue_number),
                    str(event_name),
                    str(source_event),
                    str(run_dir),
                    str(data_json or "{}"),
                )
            )

        replacements: dict[str, tuple[Path, int]] = {}
        for (
            _sequence,
            issue_number,
            _event_name,
            _source_event,
            original_run_dir,
            _data_json,
        ) in rows:
            if original_run_dir not in replacements:
                run_index = len(replacements) + 1
                synthetic_run_dir = (
                    run_dir_root
                    / f"worktree-{run_index}"
                    / ".issue-orchestrator"
                    / "sessions"
                    / f"fixture-{run_index}__session-{run_index}"
                )
                _write_minimal_terminal_recording(
                    synthetic_run_dir,
                    issue_number=issue_number,
                )
                replacements[original_run_dir] = (synthetic_run_dir, issue_number)
            elif replacements[original_run_dir][1] != issue_number:
                raise ValueError(
                    "Captured Timeline run belongs to multiple issues: "
                    f"run_dir={original_run_dir}"
                )

        # Rows are materialized before UPDATE so this loop does not mutate a
        # cursor while iterating over it.
        for (
            sequence,
            _issue_number,
            event_name,
            source_event,
            original_run_dir,
            data_json,
        ) in rows:
            synthetic_run_dir = replacements[original_run_dir][0]
            data = json.loads(data_json)
            if isinstance(data, dict):
                data["run_dir"] = str(synthetic_run_dir)
                canonical_event_name = source_event or event_name
                role = FIXTURE_REVIEW_PHASE_RECORDING_ROLES.get(event_name)
                round_index = data.get("round_index")
                if role is not None and isinstance(round_index, int):
                    _write_review_phase_terminal_recording(
                        synthetic_run_dir,
                        round_index=round_index,
                        role=role,
                    )
                for field in TimelineFixturePathField:
                    value = data.get(field.value)
                    if not isinstance(value, str):
                        continue
                    rewritten_path = rewrite_timeline_fixture_path(
                        field=field,
                        run_dir=synthetic_run_dir,
                        original_value=value,
                        event_name=canonical_event_name,
                        event_data=data,
                    )
                    data[field.value] = str(rewritten_path)
            conn.execute(
                "UPDATE timeline_events SET run_dir = ?, data_json = ? WHERE sequence = ?",
                (str(synthetic_run_dir), json.dumps(data), sequence),
            )
        conn.commit()


def _write_minimal_terminal_recording(
    run_dir: Path,
    *,
    issue_number: int,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    recording_path = run_dir / "terminal-recording.jsonl"
    payload = base64.b64encode(b"integration fixture session\n").decode("ascii")
    recording_path.write_text(
        json.dumps(
            {
                "event_type": "output",
                "offset_ms": 0,
                "data_b64": payload,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    write_available_timeline_run_manifest(
        run_dir=run_dir,
        terminal_recording=recording_path,
        issue_number=issue_number,
    )


def _write_review_phase_terminal_recording(
    run_dir: Path,
    *,
    round_index: int,
    role: str,
) -> None:
    recording = (
        run_dir
        / "review-exchange"
        / f"round-{round_index:03d}"
        / role
        / "terminal-recording.jsonl"
    )
    recording.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.b64encode(
        f"integration fixture {role} round {round_index}\n".encode()
    ).decode(
        "ascii",
    )
    output = json.dumps(
        {
            "event_type": "output",
            "offset_ms": 0,
            "data_b64": payload,
        },
        sort_keys=True,
    )
    recording.write_text(
        f"{output}\n",
        encoding="utf-8",
    )


def _stage_fixture_with_real_timeline_reader(
    fixture: Path,
    tmp_path: Path,
):
    """Stage a fixture and return (repo_root, mock_orchestrator).

    The orchestrator gets a real ``timeline_reader`` over the staged base
    timeline so the click-through path through ``get_issue_detail`` runs
    against actual on-disk data.
    """
    from unittest.mock import MagicMock
    from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
    from issue_orchestrator.execution.timeline_store import SqliteTimelineStore

    repo_root = _stage_fixture_layout(fixture, tmp_path)
    base_store = SqliteTimelineStore(
        db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )

    mock_orch = MagicMock()
    mock_orch.config.repo_root = repo_root
    mock_orch.deps.timeline_store = base_store
    mock_orch.deps.timeline_reader = DefaultTimelineReader(base_store)
    return repo_root, mock_orch


def _affordance_issue_numbers(evt: dict) -> list[int]:
    """Extract sorted issue numbers from an event's ``issue_affordances`` list."""
    affordances = evt.get("issue_affordances") or []
    return sorted(a["issue_number"] for a in affordances)


def _expected_issue_numbers(entry: dict) -> list[int]:
    """Extract sorted issue numbers from an expected.json entry."""
    affordances = entry.get("issue_affordances") or []
    return sorted(a["issue_number"] for a in affordances)


def _fixture_issue_numbers(expected: dict) -> list[int]:
    issue_numbers: list[int] = []
    for entry in expected["tests"]:
        for issue_number in _expected_issue_numbers(entry):
            if issue_number not in issue_numbers:
                issue_numbers.append(issue_number)
    return issue_numbers


@lru_cache(maxsize=1)
def _openapi_schema() -> dict:
    schema = json.loads(Path("docs/api/ui-openapi.json").read_text())
    return schema


@lru_cache(maxsize=None)
def _openapi_validator(component: str) -> Draft202012Validator:
    schema = _openapi_schema()
    resolver = RefResolver.from_schema(schema)
    return Draft202012Validator(
        schema["components"]["schemas"][component],
        resolver=resolver,
    )


def _schema_error_messages(errors: list[JsonSchemaValidationError]) -> str:
    messages: list[str] = []
    pending = list(errors)
    while pending:
        error = pending.pop()
        messages.append(error.message)
        pending.extend(error.context)
    return "\n".join(messages)


def _assert_matches_openapi(component: str, payload: dict) -> None:
    errors = sorted(_openapi_validator(component).iter_errors(payload), key=str)
    assert not errors, _schema_error_messages(errors)


def _get_route_payload(client: TestClient, path: str, **params: object) -> dict:
    response = client.get(path, params=params)
    assert response.status_code == 200, (
        f"GET {path} returned HTTP {response.status_code}: {response.text}"
    )
    payload = response.json()
    assert isinstance(payload, dict), (
        f"GET {path} returned non-object JSON: {payload!r}"
    )
    return payload


@pytest.mark.parametrize(
    "fixture",
    _discover_fixtures(),
    ids=lambda f: f.name,
)
def test_e2e_run_detail_matches_captured_fixture(fixture: Path, tmp_path: Path) -> None:
    """Replay a captured E2E run through /api/e2e-run-detail and diff vs ground truth.

    For each fixture under ``tests/fixtures/e2e_runs/``, we stage a real
    repo_root + e2e-worktree layout in ``tmp_path``, point a mock
    orchestrator at it, hit the production endpoint via TestClient, and
    assert each test row's ``issue_affordances`` matches ``expected.json``.

    A failure here means either:
      a) the matcher / reader / view-model pipeline regressed, OR
      b) the contract LEGITIMATELY changed and the fixture needs to be
         re-blessed via ``scripts/snapshot_e2e_run.py``.

    A diff in the assertion error tells you exactly which test row drifted.
    """
    expected = json.loads((fixture / "expected.json").read_text())
    run_id = expected["run_id"]

    repo_root, mock_orch = _stage_fixture_with_real_timeline_reader(fixture, tmp_path)

    set_orchestrator(mock_orch)
    try:
        client = TestClient(app)
        response = client.get(f"/api/e2e-run-detail/{run_id}")
        assert response.status_code == 200, (
            f"Expected 200 from /api/e2e-run-detail/{run_id}, got "
            f"{response.status_code}: {response.text[:300]}"
        )
        payload = response.json()
        events = payload.get("events", [])

        # Build (nodeid -> [issue_number, ...]) from the response, taking
        # test_started events as the canonical row identity.
        actual: dict[str, list[int]] = {}
        for evt in events:
            if evt.get("event") != "e2e.test_started":
                continue
            nodeid = evt.get("nodeid", "")
            if not nodeid:
                continue
            actual[nodeid] = _affordance_issue_numbers(evt)

        # Also verify every affordance carries the correct run_id — the
        # frontend uses this to route click-throughs.
        run_id_drift: list[str] = []
        for evt in events:
            if evt.get("event") not in ("e2e.test_started", "e2e.test_completed"):
                continue
            for a in evt.get("issue_affordances") or []:
                if a.get("run_id") != run_id:
                    run_id_drift.append(
                        f"  {evt.get('nodeid', '?')} #{a.get('issue_number')}: "
                        f"run_id={a.get('run_id')} expected {run_id}"
                    )

        # Diff every expected row.
        mismatches: list[str] = []
        for entry in expected["tests"]:
            nodeid = entry["nodeid"]
            want = _expected_issue_numbers(entry)
            got = actual.get(nodeid)
            if got is None:
                mismatches.append(
                    f"  MISSING test_started for {nodeid} (expected issues={want})"
                )
            elif got != want:
                mismatches.append(
                    f"  {nodeid}\n    expected={want}\n    actual  ={got}"
                )

        # Also catch unexpected new test rows so the fixture stays canonical.
        expected_nodeids = {t["nodeid"] for t in expected["tests"]}
        unexpected = sorted(set(actual.keys()) - expected_nodeids)
        for nodeid in unexpected:
            mismatches.append(
                f"  UNEXPECTED test_started for {nodeid} (issues={actual[nodeid]})"
            )

        assert not mismatches, (
            f"Fixture {fixture.name} drift "
            f"({len(mismatches)} row(s)):\n"
            + "\n".join(mismatches)
            + "\n\nIf this is a legitimate contract change, re-bless via:"
            f"\n  scripts/snapshot_e2e_run.py --run-id {run_id} --repo-root <path>"
        )
        assert not run_id_drift, (
            f"Affordances in fixture {fixture.name} carry the wrong run_id "
            f"(frontend click-through routing will break):\n" + "\n".join(run_id_drift)
        )
    finally:
        set_orchestrator(None)


@pytest.mark.parametrize(
    "fixture",
    _discover_fixtures(),
    ids=lambda f: f.name,
)
def test_real_route_payloads_match_ui_openapi_schemas(
    fixture: Path,
    tmp_path: Path,
) -> None:
    """Validate captured real route responses against the generated UI contract."""
    expected = json.loads((fixture / "expected.json").read_text())
    run_id = expected["run_id"]
    issue_numbers = _fixture_issue_numbers(expected)
    if not issue_numbers:
        pytest.skip(f"Fixture {fixture.name} has no issue affordances to validate")

    repo_root, mock_orch = _stage_fixture_with_real_timeline_reader(fixture, tmp_path)

    set_orchestrator(mock_orch)
    set_control_orchestrator(mock_orch)
    try:
        client = TestClient(app)

        timeline_payload = _get_route_payload(
            client,
            f"/control/e2e/run/{run_id}/timeline",
            repo_root=str(repo_root),
            view="user",
        )
        _assert_matches_openapi("E2ERunTimelinePayload", timeline_payload)
        E2ERunTimelinePayload.model_validate(timeline_payload)
        assert timeline_payload["events"], (
            "real fixture timeline route returned no events"
        )
        assert isinstance(timeline_payload["cycles"], list)
        assert timeline_payload["phase_toc"], (
            "real fixture timeline route returned no phase_toc"
        )
        assert timeline_payload["issue_affordances"], (
            "real fixture timeline route returned no issue affordances"
        )
        assert any(
            event.get("issue_affordances") for event in timeline_payload["events"]
        ), "typed E2E events did not carry nested issue affordances"

        run_detail_payload = _get_route_payload(
            client,
            f"/api/e2e-run-detail/{run_id}",
        )
        _assert_matches_openapi("E2ERunDetailPayload", run_detail_payload)
        E2ERunDetailPayload.model_validate(run_detail_payload)
        assert run_detail_payload["events"]
        assert isinstance(run_detail_payload["cycles"], list)
        assert run_detail_payload["issue_affordances"]

        for issue_number in issue_numbers:
            e2e_issue_payload = _get_route_payload(
                client,
                f"/api/e2e-run/{run_id}/issue-detail/{issue_number}",
                view="user",
            )
            _assert_matches_openapi("IssueDetailPayload", e2e_issue_payload)
            IssueDetailPayload.model_validate(e2e_issue_payload)
            assert e2e_issue_payload["lifecycle"] is not None
            assert e2e_issue_payload["events"]
            assert e2e_issue_payload["lifecycle"]["current"]["issue_lifecycles"][0][
                "cycles"
            ], "semantic issue lifecycle did not carry cycles"

            dashboard_issue_payload = _get_route_payload(
                client,
                f"/api/issue-detail/{issue_number}",
            )
            _assert_matches_openapi("IssueDetailPayload", dashboard_issue_payload)
            IssueDetailPayload.model_validate(dashboard_issue_payload)
            assert dashboard_issue_payload["lifecycle"] is None
    finally:
        set_orchestrator(None)
        set_control_orchestrator(None)


def test_at_least_one_fixture_exists() -> None:
    """Sanity check: there must be at least one captured fixture."""
    fixtures = _discover_fixtures()
    assert fixtures, (
        "No E2E run fixtures found. Capture one with:\n"
        "  scripts/snapshot_e2e_run.py --run-id <id> --repo-root <path>"
    )


@pytest.mark.parametrize(
    "fixture",
    _discover_fixtures(),
    ids=lambda f: f.name,
)
def test_issue_affordance_click_through_returns_real_events(
    fixture: Path,
    tmp_path: Path,
) -> None:
    """Sequenced HTTP commands: timeline → click → issue detail.

    Pins the full navigation contract a user follows when they click
    an issue affordance in the E2E run drawer:

    1. GET /api/e2e-run-detail/{run_id}
       → discover ``issue_affordances`` (each has ``issue_number`` + ``run_id``)
    2. for each affordance, GET /api/e2e-run/{run_id}/issue-detail/{N}
       → assert the click-through resolves to a payload with real events

    The dashboard JS does exactly the same routing: affordances from the
    E2E run drawer carry the ``run_id``, and ``openIssueDetail`` uses
    it to hit the explicit e2e endpoint instead of the main
    ``/api/issue-detail/{N}`` (which only sees the base repo timeline).

    This is the "command pattern" alternative to the Playwright browser
    test: it exercises the exact HTTP path the dashboard JS follows on
    click, but without launching Chromium. Both layers should pass.
    """
    expected = json.loads((fixture / "expected.json").read_text())
    run_id = expected["run_id"]

    # Pick the distinct affordances captured under expected.json.
    # Skip fixtures that have no agent activity (e.g. run_86 — pruned worktree).
    click_through_targets = _fixture_issue_numbers(expected)
    if not click_through_targets:
        pytest.skip(f"Fixture {fixture.name} has no issue affordances to click")

    repo_root, mock_orch = _stage_fixture_with_real_timeline_reader(fixture, tmp_path)

    set_orchestrator(mock_orch)
    try:
        client = TestClient(app)

        # Command 1: pull the run drawer payload (matches what
        # showUnifiedRunView fetches in dashboard.js).
        run_response = client.get(f"/api/e2e-run-detail/{run_id}")
        assert run_response.status_code == 200
        run_payload = run_response.json()

        # Verify the run payload actually contains the affordances we
        # plan to click — guards against the test silently passing if
        # the matcher regresses.
        rendered_affordances: dict[int, int] = {}  # issue_number -> run_id
        for evt in run_payload.get("events", []):
            for a in evt.get("issue_affordances") or []:
                rendered_affordances[a["issue_number"]] = a["run_id"]
        for target in click_through_targets:
            assert target in rendered_affordances, (
                f"Run payload is missing issue {target}; the matcher dropped "
                f"an affordance the click-through test was about to verify."
            )

        # Command 2: simulate clicking each affordance. The dashboard JS
        # calls /api/e2e-run/{run_id}/issue-detail/{N}?view=user — the
        # explicit endpoint. It MUST return 200 with real events for
        # every affordance the run drawer rendered, or the click-through
        # is broken UX (empty drawer on click).
        broken: list[str] = []
        for issue_num in click_through_targets:
            affordance_run_id = rendered_affordances[issue_num]
            detail_response = client.get(
                f"/api/e2e-run/{affordance_run_id}/issue-detail/{issue_num}",
                params={"view": "user"},
            )
            if detail_response.status_code != 200:
                broken.append(
                    f"  #{issue_num} (run {affordance_run_id}): "
                    f"HTTP {detail_response.status_code} "
                    f"body={detail_response.text[:200]}"
                )
                continue
            detail = detail_response.json()
            if detail.get("issue_number") != issue_num:
                broken.append(
                    f"  #{issue_num}: payload issue_number = "
                    f"{detail.get('issue_number')!r}"
                )
                continue
            events = detail.get("events") or []
            if not events:
                broken.append(
                    f"  #{issue_num}: drawer opened but events=[]; "
                    f"the explicit e2e endpoint returned an empty payload. "
                    f"Check the view-filter at the view-model layer."
                )

        assert not broken, (
            f"Click-through navigation broken for {len(broken)} issue(s) in "
            f"fixture {fixture.name}:\n" + "\n".join(broken)
        )

        # Also verify the LEGACY /api/issue-detail/{N} endpoint does NOT
        # try to serve e2e issues — that endpoint now refuses to reach
        # into the worktree and should return the main orchestrator's
        # (empty) view for these ephemeral issues.
        for issue_num in click_through_targets:
            legacy_response = client.get(f"/api/issue-detail/{issue_num}")
            assert legacy_response.status_code == 200
            legacy_payload = legacy_response.json()
            legacy_events = legacy_payload.get("events") or []
            assert legacy_events == [], (
                f"Legacy /api/issue-detail/{issue_num} should NOT surface "
                f"e2e-worktree events — that's the explicit endpoint's job. "
                f"If you see events here, the fallback sneaked back in."
            )
            assert legacy_payload["lifecycle"] is None
    finally:
        set_orchestrator(None)


@pytest.mark.parametrize(
    "fixture",
    _discover_fixtures(),
    ids=lambda f: f.name,
)
def test_fixture_contains_no_machine_specific_paths(fixture: Path) -> None:
    """Standing guardrail: every committed fixture must be sanitized.

    Walks the fixture's sqlite files and asserts none of them carry raw
    ``/Users/<user>``, ``/private/tmp``, ``/private/var/folders``, or
    ``e2e-<random-uuid>-worktrees`` patterns. The same check runs as a
    hard guardrail at the end of ``scripts/snapshot_e2e_run.py``, but
    pinning it here as well means a developer adding a fixture by hand
    (or with a stale snapshot script) cannot bypass the contract.

    If this fails, re-snapshot the fixture::

        scripts/snapshot_e2e_run.py --run-id <id> --repo-root <path>

    Or, if a new path family needs scrubbing, add a pattern to
    ``_GENERIC_SANITIZATION_PATTERNS`` and ``_FORBIDDEN_FIXTURE_PATTERNS``
    in ``scripts/snapshot_e2e_run.py`` and re-snapshot.
    """
    # Import lazily so this test does not require scripts/ to be on
    # sys.path at collection time.
    import importlib.util

    script_path = (
        Path(__file__).parent.parent.parent / "scripts" / "snapshot_e2e_run.py"
    )
    spec = importlib.util.spec_from_file_location("snapshot_e2e_run", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    problems = module.verify_fixture_clean(fixture)
    assert not problems, (
        f"Fixture {fixture.name} contains machine-specific data:\n"
        + "\n".join(f"  - {p}" for p in problems)
    )
