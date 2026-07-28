"""Forward activity logging/reporting (offline-testable, rich-rendered).

Tracks per-forwarded file: name, type, timestamp. Renders a summary with
emoji + color via rich: total count, breakdown by type, file names, and a
rolling 5-minute window of recent forwards.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

WINDOW_MINUTES = 5


def _ext(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".") or "unknown"


class ForwardLogger:
    def __init__(self) -> None:
        self.records: list[dict] = []  # {name, type, ts:datetime}

    def record(self, name: str, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        self.records.append({"name": name, "type": _ext(name), "ts": ts})

    def count(self) -> int:
        return len(self.records)

    def by_type(self) -> dict:
        return dict(Counter(r["type"] for r in self.records))

    def recent_window(self, minutes: int = WINDOW_MINUTES) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        return [r for r in self.records if r["ts"] >= cutoff]

    def render(self, console: Console | None = None) -> None:
        console = console or Console()
        if not self.records:
            console.print("[yellow]⚠️  No files forwarded.[/yellow]")
            return

        total = self.count()
        types = self.by_type()
        window = self.recent_window()

        console.print(f"\n[bold cyan]📊 Forward Report[/bold cyan] {'=' * 20}")
        console.print(f"[green]✅ Total files:[/green] {total}")
        console.print(
            f"[blue]⏱️  Last {WINDOW_MINUTES}m:[/blue] {len(window)} file(s)"
        )

        t = Table(show_header=True, header_style="bold magenta")
        t.add_column("📁 Type", style="cyan")
        t.add_column("🔢 Count", justify="right", style="green")
        for ext, n in sorted(types.items(), key=lambda x: -x[1]):
            icon = {
                "png": "🖼️", "jpg": "🖼️", "jpeg": "🖼️", "gif": "🖼️",
                "pdf": "📄", "mp4": "🎬", "avi": "🎬", "mov": "🎬", "mkv": "🎬",
                "mp3": "🎵", "ogg": "🎵", "doc": "📝", "docx": "📝",
            }.get(ext, "📦")
            t.add_row(f"{icon} {ext}", str(n))
        console.print(t)

        console.print("[bold yellow]📝 Forwarded file names:[/bold yellow]")
        for r in self.records[-15:]:
            console.print(f"  • [white]{r['name']}[/white] [dim]({r['type']})[/dim]")
        if total > 15:
            console.print(f"  [dim]… and {total - 15} more[/dim]")
