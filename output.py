import shutil
from pathlib import Path
from typing import List

from rich.console import Console
from rich.table import Table


console = Console()


def print_matches(matches: List[Path], total: int) -> None:
    """Print a rich table of matched images, then a summary line."""
    if not matches:
        console.print(f"\n[yellow]No matches found[/yellow] in {total} image(s) scanned.")
        return

    table = Table(title=f"Matched Images ({len(matches)} of {total})")
    table.add_column("Filename", style="cyan")
    table.add_column("Cache Path", style="dim")

    for path in matches:
        table.add_row(path.name, str(path))

    console.print(table)
    console.print(f"\n[green]✓[/green] {len(matches)} of {total} image(s) matched.")


def copy_matches(matches: List[Path], output_dir: Path) -> None:
    """Copy matched image files into output_dir, creating it if needed."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for path in matches:
        shutil.copy2(path, output_dir / path.name)
    console.print(
        f"[green]✓[/green] Copied {len(matches)} file(s) to [bold]{output_dir}[/bold]"
    )
