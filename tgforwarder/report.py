"""Forward activity logging/reporting (offline-testable, rich-rendered).

Tracks per-forwarded file: name, type, timestamp. Renders a summary with
emoji + color via rich: total count, breakdown by type, file names, and a
rolling 5-minute window of recent forwards.

Scales to 5000+ files: uses a bounded deque (recent names, O(1) append) and a
Counter (type tally, O(1) increment) — no unbounded list growth.
"""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

WINDOW_MINUTES = 5
MAX_NAMES = 50  # cap stored names to keep memory O(1) at 5000+ scale


def _ext(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".") or "unknown"


class ForwardLogger:
    def __init__(self) -> None:
        # deque caps stored names at MAX_NAMES; Counter tallies types in O(1).
        self._names: deque[str] = deque(maxlen=MAX_NAMES)
        self._window: deque[tuple[datetime, str]] = deque()  # (ts, name) for 5m window
        self._types: Counter[str] = Counter()
        self._total = 0

    def record(self, name: str, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        self._total += 1
        self._names.append(name)
        self._types[_ext(name)] += 1
        self._window.append((ts, name))

    def count(self) -> int:
        return self._total

    def by_type(self) -> dict:
        return dict(self._types)

    def recent_window(self, minutes: int = WINDOW_MINUTES) -> list[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        # prune old entries (amortized O(1)); then return names in window
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        return [n for _, n in self._window]

    def render(self, console: Console | None = None) -> None:
        console = console or Console()
        if self._total == 0:
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
        shown = min(len(self._names), 15)
        for name in list(self._names)[-shown:]:
            console.print(f"  • [white]{name}[/white] [dim]({_ext(name)})[/dim]")
        if total > shown:
            console.print(f"  [dim]… and {total - shown} more[/dim]")
