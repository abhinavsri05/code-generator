"""CLI entry point for the code generator."""

import click
from .agent import run


@click.command()
@click.argument("prompt")
@click.option(
    "--cwd",
    default=".",
    show_default=True,
    help="Working directory (path to the target repository).",
)
def main(prompt: str, cwd: str) -> None:
    """Generate code using Claude with JIRA and Git MCP context.

    PROMPT is the task description or JIRA ticket key (e.g. 'PROJ-123' or
    'Implement a user login endpoint per PROJ-42').
    """
    result = run(prompt, cwd=cwd)
    click.echo(result)


if __name__ == "__main__":
    main()
