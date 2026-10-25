"""Generated files live beside the source app or portable executable."""
from pathlib import Path
from app_paths import APP_DIRECTORY

DIRECTORY = APP_DIRECTORY / "pelican"
RESULTS_PATH = DIRECTORY / "results"
DIAGNOSTICS_PATH = DIRECTORY / "diagnostics"


def migrate_legacy_files(root: Path = DIRECTORY.parent) -> None:
    """Collect files left by older builds without replacing existing results.

    An older running app may recreate the old results folder or keep its log
    open. Retry those files on the next launch; never stop the user's app.
    """
    root = Path(root)
    target = root / "pelican"
    moves = {
        "pelican_results": "results",
        "pelican.html": "examples/pelican.html",
        "pelican-ride.html": "examples/pelican-ride.html",
        "pelican_bicycle_animation.html": "examples/pelican_bicycle_animation.html",
        "preview-errors.log": "diagnostics/legacy-preview-errors.log",
        "native-preview-check.png": "diagnostics/native-preview-check.png",
    }

    def collect(source: Path, destination: Path) -> None:
        if not source.exists():
            return
        if source.is_dir() and destination.is_dir():
            for child in source.iterdir():
                collect(child, destination / child.name)
            try:
                source.rmdir()
            except OSError:
                pass
        elif not destination.exists():
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.rename(destination)
            except OSError:
                pass
        # When both copies exist, preserve both. The old process may still be
        # writing its copy; no result is overwritten or removed here.

    for old, new in moves.items():
        collect(root / old, target / new)
