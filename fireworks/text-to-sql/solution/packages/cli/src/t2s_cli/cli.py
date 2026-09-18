"""Click entry point for the ``t2s`` command line interface.

Subcommands (``query``, ``schema``, ``db create|load|query``, ``eval run``) are
added in W7 (see memory/workplan.md). This scaffold wires up an empty group so
``t2s --help`` and the console-script entry point resolve correctly from the
moment the workspace is installed.
"""

import click


@click.group()
def cli() -> None:
    """t2s — text-to-SQL command line interface."""


if __name__ == "__main__":
    cli()
