import sys


def main() -> int:
    if "--cli" in sys.argv[1:]:
        from .cli import run
        return run([a for a in sys.argv[1:] if a != "--cli"])
    from .gui.main_window import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
