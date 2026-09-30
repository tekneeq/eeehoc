"""eeehoc — NHL Live chiclets dashboard."""

__all__ = ["main"]
__version__ = "0.1.0"


def main(argv: list[str] | None = None) -> None:
    from eeehoc.cli import main as _main

    _main(argv)
