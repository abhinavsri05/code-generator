"""JIRA REST API helpers."""

import base64
import json
import re
import urllib.request

from .config import settings


def extract_issue_key(text: str) -> str | None:
    """Extract the first JIRA issue key (e.g. SCRUM-1) from a string."""
    match = re.search(r"\b([A-Z][A-Z0-9_]+-\d+)\b", text)
    return match.group(1) if match else None


def _auth_headers() -> dict:
    credentials = base64.b64encode(
        f"{settings.jira_username}:{settings.jira_api_token}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def post_comment(issue_key: str, body: str) -> None:
    """Post a plain-text comment on a JIRA issue."""
    url = f"{settings.jira_url.rstrip('/')}/rest/api/3/issue/{issue_key}/comment"

    # JIRA API v3 requires Atlassian Document Format (ADF).
    # Split on newlines and interleave hardBreak nodes so they render visibly.
    lines = body.split("\n")
    inline_content: list[dict] = []
    for i, line in enumerate(lines):
        if line:
            inline_content.append({"type": "text", "text": line})
        if i < len(lines) - 1:
            inline_content.append({"type": "hardBreak"})

    payload = json.dumps({
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": inline_content,
                }
            ],
        }
    }).encode()

    req = urllib.request.Request(url, data=payload, headers=_auth_headers(), method="POST")
    with urllib.request.urlopen(req):
        pass
