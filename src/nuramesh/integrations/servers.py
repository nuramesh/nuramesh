"""Reference servers (R2.05 rule 15): deterministic in-memory servers that satisfy the pack
contracts, for tests and template reference runs. Fixed synthetic data only (`Example Customer`,
`T-1001`, `acme/demo`, addresses on `example.test`); no clock, no randomness, no process
settings, no file, no network. They declare `transport_kind` `in_process` and are not connectors.

`list_tools()` returns plain wire-tool mappings (the object the definition hash is computed from);
`call_tool(name, arguments)` returns a mapping, or `{"isError": True, ...}` for a tool error.
"""

import inspect
from typing import Any, Final

from nuramesh.engine.integrations import load_packs

CAP: Final = 50
Result = dict[str, Any]

_TICKETS = [
    {"id": "T-1001", "queue": "support", "status": "open", "subject": "Example question"},
    {"id": "T-1002", "queue": "support", "status": "closed", "subject": "Example thanks"},
]
_CONTACTS = [
    {"id": "C-1", "name": "Example Customer", "email": "customer@example.test", "account": "A-1"}
]
_ACCOUNTS = [{"id": "A-1", "name": "Example Account"}]
_PRS = {7: {"base": "main", "title": "Example change", "files": ["README.md"]}}
_ISSUES = [{"id": "I-1", "project": "demo", "title": "Example issue", "status": "open"}]


def _error(text: str) -> Result:
    return {"isError": True, "text": text}


class ReferenceServer:
    """Base: one pack's tools over fixed data."""

    pack = ""
    transport_kind = "in_process"

    def __init__(self, description_suffix: str = "") -> None:
        self._suffix = description_suffix
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_tools(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in load_packs()[self.pack].tools:
            out.append(
                {
                    "name": tool.tool,
                    "description": f"Reference {self.pack} tool {tool.tool}.{self._suffix}",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            a: {"type": "integer" if a == "number" else "string"}
                            for a in tool.allowed_args
                        },
                    },
                }
            )
        return out

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Result:
        self.calls.append((name, dict(arguments)))
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return _error("unknown tool")
        try:
            inspect.signature(handler).bind(**arguments)
        except TypeError:  # only the argument binding; a handler's own TypeError is a bug
            return _error("the arguments do not match the tool")
        result: Result = handler(**arguments)
        return result


class GithubServer(ReferenceServer):
    pack = "github"

    def __init__(self, description_suffix: str = "") -> None:
        super().__init__(description_suffix)
        self.merged: list[int] = []

    def _t_get_pull_request(self, repo: str, number: int) -> Result:
        pr = _PRS.get(number)
        return _error("not found") if pr is None else {"number": number, "base": pr["base"]}

    def _t_list_pull_request_files(self, repo: str, number: int) -> Result:
        pr = _PRS.get(number)
        return _error("not found") if pr is None else {"items": list(pr["files"])[:CAP]}

    def _t_get_check_runs(self, repo: str, ref: str) -> Result:
        return {"items": [{"name": "tests", "conclusion": "success"}][:CAP]}

    def _t_comment_on_pull_request(self, repo: str, number: int, body: str) -> Result:
        return {"ok": True}

    def _t_create_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> Result:
        return {"number": 8}

    def _t_merge_pull_request(self, repo: str, number: int, branch: str) -> Result:
        pr = _PRS.get(number)
        if pr is None:
            return _error("the base of the pull request cannot be determined")
        if pr["base"] != branch:
            return _error("the base branch is not the one claimed")
        self.merged.append(number)
        return {"merged": number}


class IssuesServer(ReferenceServer):
    pack = "issues"

    def _t_search_issues(self, project: str, query: str) -> Result:
        return {"items": [i for i in _ISSUES if i["project"] == project][:CAP]}

    def _t_get_issue(self, project: str, id: str) -> Result:
        found = [i for i in _ISSUES if i["id"] == id and i["project"] == project]
        return found[0] if found else _error("not found")

    def _t_create_issue(self, project: str, title: str, body: str) -> Result:
        return {"id": "I-2"}

    def _t_update_issue_status(self, project: str, id: str, status: str) -> Result:
        return {"ok": True}

    def _t_add_issue_comment(self, project: str, id: str, body: str) -> Result:
        return {"ok": True}


class HelpdeskServer(ReferenceServer):
    pack = "helpdesk"

    def _t_list_tickets(self, queue: str, status: str) -> Result:
        items = [
            t for t in _TICKETS if queue in ("all", t["queue"]) and status in ("any", t["status"])
        ]
        return {"items": items[:CAP]}

    def _t_get_ticket(self, ticket_id: str) -> Result:
        found = [t for t in _TICKETS if t["id"] == ticket_id]
        return found[0] if found else _error("not found")

    def _t_add_internal_note(self, ticket_id: str, body: str) -> Result:
        return {"ok": True}

    def _t_reply_to_ticket(self, ticket_id: str, body: str) -> Result:
        return {"ok": True}

    def _t_set_ticket_status(self, ticket_id: str, status: str) -> Result:
        return {"ok": True}


class CrmServer(ReferenceServer):
    pack = "crm"

    def _t_search_contacts(self, query: str) -> Result:
        return {"items": [c for c in _CONTACTS if query.lower() in c["name"].lower()][:CAP]}

    def _t_get_contact(self, contact_id: str) -> Result:
        found = [c for c in _CONTACTS if c["id"] == contact_id]
        return found[0] if found else _error("not found")

    def _t_get_account(self, account_id: str) -> Result:
        found = [a for a in _ACCOUNTS if a["id"] == account_id]
        return found[0] if found else _error("not found")

    def _t_log_note(self, contact_id: str, body: str) -> Result:
        return {"ok": True}

    def _t_update_contact_field(self, contact_id: str, field: str, value: str) -> Result:
        return {"ok": True}

    def _t_create_followup_task(self, contact_id: str, title: str, due: str) -> Result:
        return {"id": "K-1"}


_SERVERS: Final = {
    "github": GithubServer,
    "issues": IssuesServer,
    "helpdesk": HelpdeskServer,
    "crm": CrmServer,
}


def reference_server(pack: str, description_suffix: str = "") -> Any:
    """A fresh reference server for a pack name."""
    return _SERVERS[pack](description_suffix)
