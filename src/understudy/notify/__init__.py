"""Notification package for Slack reasoning posts and PagerDuty escalations."""

from understudy.notify.api import Notifier
from understudy.notify.fakes import FakeNotifier
from understudy.notify.slack import (
    SLACK_API_URL,
    SlackNotifier,
    build_candidate_table,
    build_decision_analysis,
    build_slack_reasoning_blocks,
    build_slack_reasoning_text,
)

__all__ = [
    "SLACK_API_URL",
    "FakeNotifier",
    "Notifier",
    "SlackNotifier",
    "build_candidate_table",
    "build_decision_analysis",
    "build_slack_reasoning_blocks",
    "build_slack_reasoning_text",
]
