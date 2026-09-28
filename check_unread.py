# /// script
# requires-python = ">=3.11"
# dependencies = ["typesafe-sdk", "python-dotenv"]
# ///
"""Triage unread Gmail inbox messages and suggest (or apply) your Gmail labels.

Labels are read from your Gmail account on every run, and each label is
described to the model by the most recent emails already filed under it, so
new labels are picked up automatically.

Messages always stay unread: bodies are fetched with BODY.PEEK. Without
--apply the inbox is opened read-only and nothing changes. With --apply the
suggested labels are added (never removed); nothing is moved or deleted.
--apply also records the highest processed UID in .triage_state.json, so
scheduled runs only look at new mail. Dry runs ignore that state.

Usage: uv run check_unread.py [--limit N] [--threshold P] [--label-threshold P] [--apply]
"""

import argparse
import asyncio
import base64
import email
import html
import imaplib
import json
import os
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Noul, Score

MAX_BODY_CHARS = 6000
EXAMPLES_PER_LABEL = 5
STATE_FILE = Path(__file__).with_name(".triage_state.json")
MSGID_RE = re.compile(rb"X-GM-MSGID (\d+)")
LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)')

IMPORTANT = Noul(
    instructions={
        "question": (
            "Is `email` important, meaning the recipient should personally read it soon?"
        ),
        "important_examples": [
            "a message written by a real person to the recipient",
            "a request that needs a reply, decision, or action",
            "a deadline, appointment, or time-sensitive change",
            "bills, payments, bank, tax, legal, medical, or travel matters",
            "security alerts, password resets, or account problems",
        ],
        "not_important_examples": [
            "newsletters, promotions, sales, and marketing",
            "social media notifications and digests",
            "routine automated receipts or shipping updates with no action needed",
            "mass mailings not addressed to the recipient personally",
        ],
    }
)

URGENCY = Score(
    instructions="How soon does the recipient need to read or act on `email`?",
    criteria=[
        "never: safe to ignore or delete",
        "whenever: useful to read eventually, no action needed",
        "this week: needs attention or action within the next few days",
        "today: needs attention or action today",
        "right now: needs immediate action, such as an active security threat or outage",
    ],
)


def decode(value: str | None) -> str:
    return str(make_header(decode_header(value))) if value else ""


def quote(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def utf7_decode(mailbox: str) -> str:
    """Decode IMAP modified UTF-7 (RFC 3501) mailbox names for display."""

    def repl(m: re.Match) -> str:
        if not m.group(1):
            return "&"
        chunk = m.group(1).replace(",", "/")
        return base64.b64decode(chunk + "=" * (-len(chunk) % 4)).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", repl, mailbox)


def body_text(msg: Message) -> str:
    """Return the plain-text body, falling back to tag-stripped HTML."""
    plain, rich = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and rich is None:
            rich = text
    if plain is None and rich is not None:
        rich = re.sub(r"(?is)<(script|style).*?</\1>", " ", rich)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", rich))
    return re.sub(r"\s+", " ", plain or "").strip()[:MAX_BODY_CHARS]


def fetch_labels(imap: imaplib.IMAP4_SSL, exclude: set[bytes]) -> list[dict]:
    """Return the user's own labels, each with recent example emails.

    Emails whose Gmail message ID is in `exclude` (the ones being classified)
    are never used as examples, so an email cannot vouch for its own label.
    """
    labels = []
    for line in imap.list()[1]:
        m = LIST_RE.match(line.decode())
        raw = m["name"].strip('"').replace('\\"', '"').replace("\\\\", "\\")
        if "\\Noselect" in m["flags"] or raw == "INBOX" or raw.startswith("[Gmail]"):
            continue
        imap.select(quote(raw), readonly=True)
        _, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split()[-(EXAMPLES_PER_LABEL + len(exclude)) :]
        examples = []
        if uids:
            _, parts = imap.uid(
                "FETCH", b",".join(uids), "(X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"
            )
            for part in parts:
                if isinstance(part, tuple) and MSGID_RE.search(part[0])[1] not in exclude:
                    msg = email.message_from_bytes(part[1])
                    examples.append(
                        {"from": decode(msg.get("From")), "subject": decode(msg.get("Subject"))}
                    )
        examples = examples[-EXAMPLES_PER_LABEL:]
        labels.append({"raw": raw, "name": utf7_decode(raw), "examples": examples})
    return labels


def load_state(uidvalidity: str) -> int:
    """Return the last processed UID, or 0 if unknown or the mailbox was rebuilt."""
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return 0
    return state["last_uid"] if state.get("uidvalidity") == uidvalidity else 0


def save_state(uidvalidity: str, last_uid: int) -> None:
    STATE_FILE.write_text(json.dumps({"uidvalidity": uidvalidity, "last_uid": last_uid}))


def fetch_unread(
    imap: imaplib.IMAP4_SSL, limit: int, writable: bool, after_uid: int = 0
) -> list[dict]:
    imap.select("INBOX", readonly=not writable)
    _, data = imap.uid("SEARCH", None, "UNSEEN")
    uids = [uid for uid in data[0].split() if int(uid) > after_uid]
    uids = uids[-limit:][::-1]  # newest first
    emails = []
    for uid in uids:
        _, parts = imap.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[])")
        msg = email.message_from_bytes(parts[0][1])
        date = msg.get("Date")
        emails.append(
            {
                "uid": uid,
                "msgid": MSGID_RE.search(parts[0][0])[1],
                "from": decode(msg.get("From")),
                "to": decode(msg.get("To")),
                "subject": decode(msg.get("Subject")),
                "date": parsedate_to_datetime(date).isoformat() if date else "",
                "body": body_text(msg),
            }
        )
    return emails


def label_question(label: dict) -> Noul:
    return Noul(
        instructions={
            "label": label["name"],
            "recent_emails_with_this_label": label["examples"],
            "question": (
                "The recipient files emails like `recent_emails_with_this_label` under "
                "the Gmail label `label`. Should `email` also get the label `label`?"
            ),
        }
    )


async def classify(emails: list[dict], labels: list[dict]) -> list[dict]:
    """Ask all questions about each email in one request; run emails concurrently."""
    questions = {"important": IMPORTANT, "urgency": URGENCY}
    questions |= {f"label:{label['raw']}": label_question(label) for label in labels}
    async with AsyncTypeSafeClient() as client:
        responses = await asyncio.gather(
            *(
                client.system_one(
                    state={"email": {k: v for k, v in e.items() if k not in ("uid", "msgid")}},
                    questions=questions,
                )
                for e in emails
            )
        )
    return [
        {
            "important": r.nouls["important"].noul,
            "urgency": r.scores["urgency"].score,
            "labels": {label["raw"]: r.nouls[f"label:{label['raw']}"].noul for label in labels},
        }
        for r in responses
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=50, help="max unread emails to check")
    parser.add_argument("--threshold", type=float, default=0.5, help="importance cutoff")
    parser.add_argument(
        "--label-threshold", type=float, default=0.7, help="min probability to suggest a label"
    )
    parser.add_argument("--apply", action="store_true", help="add suggested labels in Gmail")
    args = parser.parse_args()

    load_dotenv()
    with imaplib.IMAP4_SSL("imap.gmail.com") as imap:
        imap.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        _, status = imap.status("INBOX", "(UIDVALIDITY)")
        uidvalidity = re.search(rb"UIDVALIDITY (\d+)", status[0])[1].decode()
        after_uid = load_state(uidvalidity) if args.apply else 0
        emails = fetch_unread(imap, args.limit, writable=args.apply, after_uid=after_uid)
        if not emails:
            print("No new unread emails." if args.apply else "No unread emails.")
            return
        labels = fetch_labels(imap, exclude={e["msgid"] for e in emails})
        imap.select("INBOX", readonly=not args.apply)  # back to the inbox for STORE
        print(f"Labels: {', '.join(label['name'] for label in labels)}")

        results = asyncio.run(classify(emails, labels))
        names = {label["raw"]: label["name"] for label in labels}
        for r in results:
            r["suggested"] = sorted(
                (raw for raw, p in r["labels"].items() if p >= args.label_threshold),
                key=lambda raw: r["labels"][raw],
                reverse=True,
            )

        ranked = sorted(zip(results, emails), key=lambda pair: pair[0]["urgency"], reverse=True)
        for heading, keep in (("IMPORTANT", True), ("NOT IMPORTANT", False)):
            group = [(r, e) for r, e in ranked if (r["important"] >= args.threshold) == keep]
            print(f"\n=== {heading} ({len(group)}) ===")
            print(f"  {'imp':>4}  {'urg':>4}  {'from':<30}  {'subject':<50}  labels")
            for r, e in group:
                suggested = ", ".join(f"{names[raw]} ({r['labels'][raw]:.2f})" for raw in r["suggested"])
                print(
                    f"  {r['important']:.2f}  {r['urgency']:.2f}  "
                    f"{e['from'][:30]:<30}  {e['subject'][:50]:<50}  {suggested or '-'}"
                )
        print("\nurg: 0=never 1=whenever 2=this week 3=today 4=right now")

        if not args.apply:
            print("Dry run: no labels changed. Rerun with --apply to add suggested labels.")
            return
        applied = 0
        for r, e in zip(results, emails):
            if r["suggested"]:
                targets = " ".join(quote(raw) for raw in r["suggested"])
                imap.uid("STORE", e["uid"], "+X-GM-LABELS", f"({targets})")
                applied += len(r["suggested"])
        save_state(uidvalidity, max(after_uid, *(int(e["uid"]) for e in emails)))
        print(f"Applied {applied} label(s). Emails remain unread.")


if __name__ == "__main__":
    main()
