"""CLI entry point for the code generator."""

import click
from .agent import run
from .jira import extract_issue_key, post_comment
from .config import settings


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

    issue_key = extract_issue_key(prompt)
    if issue_key and settings.jira_url and settings.jira_api_token:
        try:
            post_comment(issue_key, result)
            click.echo(f"\nPosted comment to {issue_key}.")
        except Exception as e:
            click.echo(f"\nWarning: could not post JIRA comment — {e}", err=True)


if __name__ == "__main__":
    main()
