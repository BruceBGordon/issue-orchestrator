"""Smoke tests that drive the real Textual/Rich runtime.

The rest of the dashboard suite mocks ``query_one``, so it covers our logic
around Textual but never mounts a widget or renders a frame. That left the
Textual/Rich runtime itself uncovered: an upgrade could change mounting,
composition, or markup rendering and every existing test would still pass.

``DashboardApp._refresh_loop`` also swallows exceptions by design, so a runtime
break there is invisible at the UI. These tests therefore mount the app through
Textual's real pilot harness and render Rich markup for real, so that a Textual
or Rich upgrade that breaks the runtime fails the suite instead of shipping.

Kept deliberately shallow -- this is dependency-surface coverage, not a
behavioral spec for the dashboard.
"""

import pytest
from rich.console import Console
from rich.table import Table

from tests.conftest import operator_paused_state
from issue_orchestrator.entrypoints.dashboard import (
    DashboardApp,
    QueueTable,
    SessionsTable,
    StatusBar,
)
from tests.unit.test_dashboard import create_orchestrator


class TestTextualRuntimeSmoke:
    """Mount the real Textual app; no query_one mocking."""

    @pytest.mark.asyncio
    async def test_app_mounts_and_composes_all_widgets(self) -> None:
        app = DashboardApp(create_orchestrator())

        async with app.run_test() as pilot:
            await pilot.pause()

            # Each composed widget actually mounted and is queryable by id.
            assert app.query_one("#status-bar", StatusBar) is not None
            assert app.query_one("#sessions", SessionsTable) is not None
            assert app.query_one("#queue", QueueTable) is not None

    @pytest.mark.asyncio
    async def test_status_bar_renders_orchestrator_state(self) -> None:
        orchestrator = create_orchestrator()
        orchestrator.state.pause_state = operator_paused_state()

        app = DashboardApp(orchestrator)
        async with app.run_test() as pilot:
            await pilot.pause()

            status_bar = app.query_one("#status-bar", StatusBar)
            # StatusBar.on_mount pushed real Rich markup through Textual's
            # renderer; if markup handling breaks, this stops reflecting state.
            rendered = str(status_bar.render())
            assert "PAUSED" in rendered

    @pytest.mark.asyncio
    async def test_data_tables_populate_columns_on_mount(self) -> None:
        from textual.widgets import DataTable

        app = DashboardApp(create_orchestrator())
        async with app.run_test() as pilot:
            await pilot.pause()

            sessions_table = app.query_one("#sessions", SessionsTable)
            data_table = sessions_table.query_one(DataTable)
            assert len(data_table.columns) > 0


class TestRichRenderingSmoke:
    """Exercise the Rich APIs the CLI actually uses."""

    def test_table_renders_rows_to_console(self) -> None:
        console = Console(record=True, width=80)
        table = Table(title="Sessions")
        table.add_column("Issue")
        table.add_column("State")
        table.add_row("M1-011", "running")

        console.print(table)
        output = console.export_text()

        assert "M1-011" in output
        assert "running" in output

    def test_console_renders_markup(self) -> None:
        console = Console(record=True, width=80)

        console.print("[green]RUNNING[/green]")
        output = console.export_text()

        # Markup is interpreted, not echoed literally.
        assert "RUNNING" in output
        assert "[green]" not in output
