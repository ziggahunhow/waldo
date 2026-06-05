import shutil
import sys
from pathlib import Path
from typing import List

import click
from dotenv import load_dotenv

load_dotenv()
from rich.console import Console
from rich.progress import track

from cache import get_cache_dir, list_cached_images
from drive import download_images, extract_folder_id
from output import copy_matches, print_matches
from recognizer import encode_references, is_match


console = Console()


@click.command()
@click.option("--url", required=True, help="Public Google Drive folder URL")
@click.option(
    "--reference",
    "references",
    required=True,
    multiple=True,
    help="Local path to a reference photo (repeat for multiple)",
)
@click.option(
    "--output",
    "output_dir",
    default="./results",
    show_default=True,
    help="Folder to copy matched images into",
)
@click.option(
    "--tolerance",
    default=0.25,
    show_default=True,
    type=click.FloatRange(0.1, 1.0),
    help="Face match threshold — lower is stricter",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Force re-download even if folder is already cached",
)
def main(
    url: str,
    references: List[str],
    output_dir: str,
    tolerance: float,
    no_cache: bool,
) -> None:
    """Find photos containing a specific person in a public Google Drive folder."""

    # 1. Parse the Drive URL
    try:
        folder_id = extract_folder_id(url)
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    # 2. Resolve cache directory
    cache_dir = get_cache_dir(folder_id)

    # 3. Optionally clear cache
    if no_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)
        console.print(f"[dim]Cache cleared for folder {folder_id}[/dim]")

    # 4. STAGE 1: Download (skip if already cached)
    cached_images = list_cached_images(cache_dir)
    if cached_images:
        console.print(f"\n[bold]Stage 1:[/bold] Using cached images in {cache_dir}")
    else:
        console.print(f"\n[bold]Stage 1:[/bold] Syncing images → {cache_dir}")
        try:
            download_images(url, cache_dir)
        except Exception as exc:
            console.print(f"[red]Download failed:[/red] {exc}")
            console.print(
                "Make sure the folder sharing is set to 'Anyone with the link can view'."
            )
            sys.exit(1)
        cached_images = list_cached_images(cache_dir)
    if not cached_images:
        console.print("[yellow]No images (.jpg/.jpeg/.png) found in the Drive folder.[/yellow]")
        sys.exit(0)

    console.print(f"[green]✓[/green] {len(cached_images)} image(s) ready.\n")

    # 5. Encode reference photos
    console.print(f"[bold]Stage 2:[/bold] Encoding {len(references)} reference photo(s)...")
    known_encodings = encode_references(list(references))
    if not known_encodings:
        console.print(
            "[red]Error:[/red] No faces detected in any reference photo. "
            "Try a clearer, well-lit photo."
        )
        sys.exit(1)
    console.print(f"[green]✓[/green] {len(known_encodings)} face encoding(s) loaded.\n")

    # 6. STAGE 3: Search
    console.print("[bold]Stage 3:[/bold] Searching for matches...")
    matches: List[Path] = []
    for image_path in track(cached_images, description="Scanning..."):
        try:
            if is_match(str(image_path), known_encodings, tolerance=tolerance):
                matches.append(image_path)
        except Exception as exc:
            console.print(f"[yellow]Skipping {image_path.name}:[/yellow] {exc}")

    # 7. Output results
    print_matches(matches, total=len(cached_images))
    if matches:
        copy_matches(matches, Path(output_dir))


if __name__ == "__main__":
    main()
