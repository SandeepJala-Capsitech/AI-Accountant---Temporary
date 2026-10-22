"""Emails to the client, from fixed templates: the agent never writes to the client itself. It gives a document
type, a period and a reason, and the system sends them.

At the end of a run, `send_document_requests` emails the client contact one link for every document the run
asked for. `send_reminders` emails again after 3 and 7 days while a document hasn't arrived. Without an SMTP
server (LEDGERSYNC_SMTP_HOST empty) each email is written to the log instead, which is how development and the
tests see them.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
import smtplib
import ssl
import threading
from collections import defaultdict
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

from ledgersync.config import Settings
from ledgersync.errors import EmailNotSent, LedgerSyncError

if TYPE_CHECKING:   # the tools import this module, so only the type checker imports them back
    from ledgersync.agent.tools import ToolDeps
    from ledgersync.models import Client

logger = logging.getLogger(__name__)

SUBJECT = "Documents needed for {client_name}'s accounts"
REMINDER_SUBJECT = "Reminder: documents needed for {client_name}'s accounts"
BODY = ("Hello {contact_name},\n\nTo finish {client_name}'s accounts for {period}, we need:\n\n{items}\n\n"
        "Upload them here: {link}\n\nThank you.")
ITEM = "- {document_type} ({period}): {reason}"

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    body: str


class Notifier(Protocol):
    """Sends one plain-text email."""

    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleNotifier:
    """Writes each email to the log and keeps it in `sent`: used when no SMTP server is set."""

    def __init__(self):
        self.sent: list[Email] = []
        self._lock = threading.Lock()

    def send(self, to: str, subject: str, body: str) -> None:
        logger.info("No SMTP server is set, so this email to %s was not sent:\n%s\n\n%s", to, subject, body)
        with self._lock:
            self.sent.append(Email(to, subject, body))


class SmtpNotifier:
    """Sends through an SMTP server over STARTTLS, logging in when a username is set."""

    def __init__(self, host: str, port: int, username: str, password: str, sender: str):
        self.host, self.port, self.username, self.sender = host, port, username, sender
        self._password = password

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self._password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("Email to %s could not be sent: %s", to, type(exc).__name__)
            raise EmailNotSent(f"The email to {to} couldn't be sent. Check the LEDGERSYNC_SMTP settings in .env, "
                               "then retry.") from None


def notifier_for(settings: Settings) -> Notifier:
    if not settings.smtp_host:
        return ConsoleNotifier()
    return SmtpNotifier(settings.smtp_host, settings.smtp_port, settings.smtp_username, settings.smtp_password,
                        settings.smtp_sender)


def long_date(day: dt.date) -> str:
    """A date as the emails write it: "1 April 2025"."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def no_email_note(client_name: str, count: int) -> str:
    """The exception (and feed line) for requests that couldn't be sent for want of an address."""
    if count == 1:
        return f"{client_name} has no contact email, so 1 document request wasn't sent."
    return f"{client_name} has no contact email, so {count} document requests weren't sent."


def _email(subject: str, client: Client, period: str, requests: list[dict], token: str, public_url: str) -> tuple[str, str]:
    items = "\n".join(ITEM.format(document_type=r["document_type"], period=r["period"], reason=r["reason"])
                      for r in requests)
    body = BODY.format(contact_name=client.contact_name or client.name, client_name=client.name, period=period,
                       items=items, link=f"{public_url.rstrip('/')}/requests/{token}")
    return subject.format(client_name=client.name), body


def _period(deps: ToolDeps, job_id: str) -> str:
    job = deps.records.get(job_id)
    return f"{long_date(job.period_start)} to {long_date(job.period_end)}"


def send_document_requests(deps: ToolDeps, client_id: int, job_id: str) -> int:
    """Emails the client contact one link for every request the job made that hasn't been sent, marks them sent,
    closes the client's older requests for the same documents (each audited as replaced), and returns how many
    were sent. Without a contact email nothing is sent and nothing closed: an exception says so, and 0 is
    returned."""
    pending = deps.requests.pending_for(job_id)
    if not pending:
        return 0
    client = deps.store.get_client(client_id)
    if not client.contact_email:
        note = no_email_note(client.name, len(pending))
        logged, new = deps.exceptions.log_once(
            client_id=client_id, job_id=job_id, stage="requests", description=note,
            assumption="The accounts are prepared without these documents until they are sent.", confidence=1.0,
            row_ids=[])
        if new:
            deps.audit.append(actor="system", action="log_exception", client_id=client_id, job_id=job_id,
                              target={"exception_id": logged["id"]}, before=None,
                              after={"description": note, "stage": "requests"})
        return 0
    token = secrets.token_urlsafe(24)
    subject, body = _email(SUBJECT, client, _period(deps, job_id), pending, token, deps.settings.public_url)
    deps.notifier.send(client.contact_email, subject, body)
    now = dt.datetime.now(dt.timezone.utc)
    deps.requests.mark_sent([r["id"] for r in pending], token=token, at=now)
    for request in pending:   # closed only now the new one is out, so the client always has a live request
        for old in deps.requests.replace_older(request, now=now):
            deps.audit.append(actor="system", action="replace_request", client_id=client_id, job_id=job_id,
                              target={"request_id": old["id"]}, before={"status": old["status"]},
                              after={"status": "replaced", "replaced_by": request["id"]})
    return len(pending)


def send_reminders(deps: ToolDeps, now: dt.datetime) -> int:
    """Emails a reminder for every link with documents due one (3 and 7 days after the email, while they
    haven't arrived): one email per link, listing its due documents. Returns how many emails went."""
    by_token: dict[str, list[dict]] = defaultdict(list)
    for request in deps.requests.due_reminders(now):
        by_token[request["token"]].append(request)
    sent = 0
    for token, requests in by_token.items():
        try:
            first = requests[0]
            client = deps.store.get_client(first["client_id"])
            if not client.contact_email or client.archived:
                continue
            due = [r for r in requests if deps.requests.mark_reminded(r, now=now)]
            if not due:
                continue
            subject, body = _email(REMINDER_SUBJECT, client, _period(deps, first["job_id"]), due, token,
                                   deps.settings.public_url)
            deps.notifier.send(client.contact_email, subject, body)
            sent += 1
        except LedgerSyncError as exc:   # one link's trouble doesn't hold up the others
            logger.warning("A reminder for client %s could not be sent: %s", requests[0]["client_id"], exc.message)
    return sent
