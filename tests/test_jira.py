"""Test JIRA connectivity by reading a story."""

import base64
import urllib.request
import json

from code_generator.config import settings


def fetch_jira_issue(issue_key: str) -> dict:
    """Fetch a JIRA issue via the REST API."""
    url = f"{settings.jira_url.rstrip('/')}/rest/api/3/issue/{issue_key}"
    print(f"\nGET {url}")
    credentials = base64.b64encode(
        f"{settings.jira_username}:{settings.jira_api_token}".encode()
    ).decode()

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def list_projects() -> list:
    """List all JIRA projects to find the right project key."""
    url = f"{settings.jira_url.rstrip('/')}/rest/api/3/project"
    credentials = base64.b64encode(
        f"{settings.jira_username}:{settings.jira_api_token}".encode()
    ).decode()
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def _adf_to_text(node: dict, indent: int = 0) -> str:
    """Recursively convert Atlassian Document Format (ADF) to plain text."""
    node_type = node.get("type", "")
    text = node.get("text", "")
    children = node.get("content", [])

    if node_type == "text":
        return text

    prefix = ""
    suffix = ""
    if node_type in ("paragraph", "heading"):
        suffix = "\n"
    elif node_type == "bulletList":
        suffix = "\n"
    elif node_type == "listItem":
        prefix = "  " * indent + "• "
        suffix = "\n"
        indent += 1
    elif node_type == "orderedList":
        suffix = "\n"
    elif node_type == "codeBlock":
        prefix = "```\n"
        suffix = "\n```\n"
    elif node_type == "blockquote":
        prefix = "> "
        suffix = "\n"

    return prefix + "".join(_adf_to_text(c, indent) for c in children) + suffix


def test_auth():
    """Verify the API token authenticates correctly."""
    url = f"{settings.jira_url.rstrip('/')}/rest/api/3/myself"
    credentials = base64.b64encode(
        f"{settings.jira_username}:{settings.jira_api_token}".encode()
    ).decode()
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read())
    print(f"\nAuthenticated as: {data.get('emailAddress')} ({data.get('displayName')})")
    assert data.get("emailAddress")


def test_list_projects():
    # /project/search returns all projects including next-gen (team-managed)
    url = f"{settings.jira_url.rstrip('/')}/rest/api/3/project/search"
    credentials = base64.b64encode(
        f"{settings.jira_username}:{settings.jira_api_token}".encode()
    ).decode()
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read())
    projects = data.get("values", [])
    print(f"\nTotal projects: {data.get('total', 0)}")
    for p in projects:
        print(f"  {p['key']}: {p['name']} (style: {p.get('style', 'unknown')})")
    assert projects, f"No projects found. Full response: {data}"


def test_read_jira_story():
    issue = fetch_jira_issue("SCRUM-1")

    fields = issue["fields"]
    print(f"\nKey:         {issue['key']}")
    print(f"Summary:     {fields['summary']}")
    print(f"Status:      {fields['status']['name']}")
    print(f"Type:        {fields['issuetype']['name']}")
    if fields.get("description"):
        print(f"Description:\n{_adf_to_text(fields['description'])}")

    assert issue["key"] == "SCRUM-1"
    assert fields["summary"]


if __name__ == "__main__":
    test_read_jira_story()
