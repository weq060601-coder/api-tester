"""Single executable entry for the main app and its embedded renderer child."""
import sys
import traceback


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--preview-renderer":
        del sys.argv[1]
        from pelican.preview import main as renderer_main
        renderer_main()
        return 0
    from api_tester import main as app_main
    return app_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # A windowed EXE has no console. Preserve startup/renderer failures in
        # its own portable data directory instead of silently disappearing.
        from app_paths import APP_DIRECTORY
        directory = APP_DIRECTORY / "pelican" / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "startup-errors.log").open("a", encoding="utf-8") as log:
            traceback.print_exc(file=log)
        raise
