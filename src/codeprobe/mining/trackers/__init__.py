"""Issue tracker adapters — fetch tickets from Jira / future trackers."""

from codeprobe.mining.trackers.base import IssueAdapter, Ticket
from codeprobe.mining.trackers.jira import JiraAdapter

__all__ = ["IssueAdapter", "JiraAdapter", "Ticket"]
