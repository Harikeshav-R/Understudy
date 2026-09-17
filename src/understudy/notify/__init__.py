"""Notification package for Slack reasoning posts and PagerDuty escalations."""

from understudy.notify.api import Notifier
from understudy.notify.composite import CompositeNotifier
from understudy.notify.fakes import FakeNotifier
from understudy.notify.pagerduty import (
    DEFAULT_FROM_EMAIL,
    PAGERDUTY_API_BASE,
    PagerDutyNotifier,
    build_pagerduty_escalation_note,
    resolve_pagerduty_incident_id,
)
from understudy.notify.slack import (
    SLACK_API_URL,
    SlackNotifier,
    build_candidate_table,
    build_decision_analysis,
    build_slack_reasoning_blocks,
    build_slack_reasoning_text,
)

__all__ = [
    "DEFAULT_FROM_EMAIL",
    "PAGERDUTY_API_BASE",
    "SLACK_API_URL",
    "CompositeNotifier",
    "FakeNotifier",
    "Notifier",
    "PagerDutyNotifier",
    "SlackNotifier",
    "build_candidate_table",
    "build_decision_analysis",
    "build_pagerduty_escalation_note",
    "build_slack_reasoning_blocks",
    "build_slack_reasoning_text",
    "resolve_pagerduty_incident_id",
]
