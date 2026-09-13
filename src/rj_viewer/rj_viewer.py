import os
import sys
import json
import duckdb
from datetime import datetime
import hashlib

from textual.app import App, ComposeResult
from textual.widgets import DataTable, RichLog, Header, Footer
from textual.containers import Container, Vertical
from textual.color import Color
from textual_plotext import PlotextPlot
from rich.syntax import Syntax
from rich.pretty import Pretty
from rich.text import Text
from rich.table import Table

GENESIS_HASH = "00000000000000000000000000000000"


class LogViewer(App):
    CSS = """
    #main-container {
        layout: vertical;
    }
    #chart-box {
        height: 10;
        border-bottom: solid $accent;
    }
    #log-table {
        height: 1fr;
        border-bottom: solid $accent;
    }
    #detail-view {
        height: 14;
        background: $surface;
    }
    """

    BINDINGS = [
        ("j", "next_row", "Next Log"),
        ("k", "prev_row", "Prev Log"),
        ("r", "reload", "Reload Folder"),
    ]

    def __init__(self, log_dir: str = "."):
        super().__init__()
        self.log_dir = log_dir
        self.db = duckdb.connect(database=":memory:")
        self.set_palette()

    def set_palette(self):
        self.theme_cols = self.app.theme_variables
        self.palette = [
            self.app.theme_variables.get("primary"),
            self.app.theme_variables.get("secondary"),
            self.app.theme_variables.get("accent"),
            self.app.theme_variables.get("warning"),
        ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main-container"):
            with Container(id="chart-box"):
                yield PlotextPlot(id="plot")
            yield DataTable(id="log-table")
            yield RichLog(id="detail-view", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Timestamp", "File", "Exit Type")

        self.theme = "rose-pine-moon"
        self.theme_changed_signal.subscribe(self, self.on_theme_change)

        self.load_all_logs()

    def load_all_logs(self) -> None:
        table = self.query_one(DataTable)
        table.clear()

        # Glob search pattern for duckdb
        glob_path = os.path.join(self.log_dir, "*.jsonl")

        # Query all JSONL files using filename metadata
        # DuckDB is doing some f'ing WEIRD stuff with how it parses the timezone
        # simultaneously converting it to UTC time but then attaching back the old
        # timezone. However, since I ended up needing the timestamp in its pure form
        # for the hash checking, pulling out the timestamp as text this way and THEN
        # converting to a timestamp properly brought it over with the correct TZ. WEIRD
        query = f"""
            SELECT 
                json ->> 'script',
                (json ->> 'timestamp')::timestamptz AS ts,
                json ->> 'timestamp' AS ts_str,
                json ->> 'status',
                json ->> 'expected',
                json ->> 'actual_result' AS actual,
                json ->> 'next_step',
                json ->> 'hash',
                json -> 'code_metrics' -> 'sloc',
                json -> 'code_metrics' -> 'functions',
                json -> 'code_metrics' -> 'max_depth',
                json ->> 'code_metrics'
            FROM read_json_objects('{glob_path}', ignore_errors=true)
            ORDER BY ts ASC
        """
        try:
            results = self.db.execute(query).fetchall()
            if not results:
                self.notify("No .jsonl files found in directory.", severity="warning")
                return

            file_colors = {}
            prev_hashes = {}
            for row in results:
                if (script := row[0]) not in file_colors:
                    file_colors[script] = self.palette[len(file_colors) % len(self.palette)]
                    prev_hashes[script] = GENESIS_HASH

            for row in results:
                (
                    filepath,
                    ts,
                    ts_str,
                    status,
                    expected,
                    actual,
                    next_step,
                    hash,
                    sloc,
                    functions,
                    depth,
                    all_metrics,
                ) = row
                all_metrics = json.loads(all_metrics)

                prev_hash = prev_hashes[filepath]
                raw_payload = f"{ts_str}|{expected}|{status}|{actual}|{next_step}|{prev_hash}|{all_metrics}"
                prev_hashes[filepath] = hash

                hash_check = hashlib.sha256(raw_payload.encode()).hexdigest()
                if hash_check != hash:
                    hash_color = self.theme_cols.get("error-darken-3")
                else:
                    hash_color = None

                if "CRASH" in status:
                    second_colon = status.find(":", 6)
                    error_type = status[:second_colon]
                    error_msg = status[second_colon + 2 :]
                    cell_status = Text(error_type, style=self.theme_cols.get("error"))
                else:
                    error_msg = None
                    cell_status = Text(status, style=self.theme_cols.get("success"))

                log_color = file_colors[filepath]
                payload = {
                    "__ts": ts_str,
                    "__Color": log_color,
                    "Expected Result": expected,
                    "Actual Result": actual,
                    "Next Step": next_step,
                    "Error Msg": error_msg,
                    "Lines of Code": sloc,
                    "Functions": functions,
                    "Depth": depth,
                }
                table.add_row(
                    Text(str(ts.astimezone()), style=log_color),
                    Text(filepath, style=f"on {hash_color}"),
                    cell_status,
                    key=json.dumps(payload),
                )

            self.render_event_plot(glob_path)

        except Exception as e:
            self.notify(f"Error loading logs: {e}", severity="error")

    def render_event_plot(self, glob_path: str) -> None:
        plot_widget = self.query_one(PlotextPlot)
        plt = plot_widget.plt
        plt.clear_data()
        plt.clear_figure()

        # Group timestamps by file source for multi-line event plot
        query = f"""
            WITH parsed AS (
                SELECT 
                    filename,
                    timestamp::timestamptz AS ts
                FROM read_json_auto('{glob_path}', filename=true, ignore_errors=true)
            )
            SELECT filename, strftime(ts, '%Y-%m-%d %H:%M:%S') AS ts
            FROM parsed
            WHERE ts IS NOT NULL
            ORDER BY filename, ts
        """
        try:
            results = self.db.execute(query).fetchall()
            if not results:
                return

            # Group events by file name
            file_events = {}
            for filename, ts in results:
                fname = os.path.basename(filename)
                file_events.setdefault(fname, []).append(ts)

            plt.date_form("Y-m-d H:M:S")

            # Build event plot per file source
            files = list(file_events.keys())
            for idx, fname in enumerate(files):
                timestamps = file_events[fname]
                color = self.palette[idx % len(self.palette)]

                # Render line of tick events using plotext.event_plot
                plt.event_plot(timestamps, color=[Color.parse(color).rgb])

            plt.title("Log Entry Distribution Timeline")
            plt.theme("textual-design-dark")
            plot_widget.refresh()

        except Exception as e:
            self.notify(f"Plot Error: {e}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Callback run whenever a new row is selected. Updates the details"""
        if event.row_key:
            self.show_full_record(event.row_key.value)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Callback run whenever a new row is highlighted. Updates the details"""
        if event.row_key:
            self.show_full_record(event.row_key.value)

    def show_full_record(self, raw_json_str: str) -> None:
        """Updates the details rich table in the RichLog block"""
        detail_view = self.query_one(RichLog)
        detail_view.clear()

        table = Table(show_header=True, expand=True, box=None)
        table.add_column("Field", no_wrap=True, width=20)
        table.add_column("Value", overflow="fold", ratio=1)

        # Parse full record and output as syntax-highlighted formatted JSON
        parsed = json.loads(raw_json_str)
        for key, val in parsed.items():
            if not key.startswith("__"):
                table.add_row(Text(key, style=parsed["__Color"]), str(val))
        detail_view.write(table)

    def on_theme_change(self, theme) -> None:
        """Built-in Textual hook triggered when app theme changes."""
        self.set_palette()
        self.load_all_logs()  # Re-renders table and plot with updated theme tokens

    def action_next_row(self) -> None:
        table = self.query_one(DataTable)
        table.action_cursor_down()

    def action_prev_row(self) -> None:
        table = self.query_one(DataTable)
        table.action_cursor_up()

    def action_reload(self) -> None:
        self.load_all_logs()

def main():
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    app = LogViewer(log_dir=target_dir)
    app.run()


if __name__ == "__main__":
    main()
