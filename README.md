# Jev Email Sorter

Sorts your unread Gmail with AI. For each unread email in your inbox, it tells you:

- **Important or not** – should you read it soon?
- **Urgency** – from "never" to "right now".
- **Labels** – which of *your own* Gmail labels fit it (for example "Amazon" or "Tax").

It can then add those labels to your emails in Gmail for you.

Your emails **stay unread**. Nothing is ever moved, archived, or deleted, and labels
are only ever added, never removed.

It uses [TypeSafe](https://typesafe.ai) (the Jev model) to make the decisions.

## How it learns your labels

You don't set up any rules. Each time it runs, it reads the labels you have in
Gmail and looks at the last few emails you filed under each one. That is how it
learns what each label means to you.

To add a new label: create it in Gmail and put a few example emails in it. The
next run will start using it.

## One-time setup

You need a Mac or PC, a Gmail account, and about 10 minutes.

### 1. Install uv

uv runs the program and installs everything it needs automatically.

- **Mac:** open the Terminal app and paste:
  ```
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **Windows:** see https://docs.astral.sh/uv/getting-started/installation/

### 2. Get a TypeSafe API key

Sign up at https://console.typesafe.ai/ and create an API key.

### 3. Create a Gmail App Password

An App Password lets this program read your Gmail without your real password.

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Type a name (for example `email-sorter`) and click **Create**.
4. Copy the 16-letter password. Google shows it only once.

### 4. Add your keys

1. In this folder, make a copy of `.env.example` and name it `.env`.
2. Open `.env` in a text editor and fill in your TypeSafe key, Gmail address,
   and App Password (without spaces).

Keep `.env` private. It is never uploaded to GitHub.

## Running it

Open Terminal in this folder and run one of these.

**Preview only** (changes nothing in Gmail):
```
uv run check_unread.py
```

**Preview and add the labels in Gmail:**
```
uv run check_unread.py --apply
```

The first run takes a little longer while it installs what it needs.

### Reading the results

```
=== IMPORTANT (1) ===
   imp   urg  from               subject                 labels
  0.90  1.85  Clint Thomas       CPA Invoice KIN-2446    Accounting (0.79)
```

- **imp** – how likely the email is important, from 0 to 1. Above 0.5 counts as important.
- **urg** – urgency: 0 = never, 1 = whenever, 2 = this week, 3 = today, 4 = right now.
- **labels** – suggested labels, with how sure it is (0 to 1).

### Good to know

- `--apply` remembers which emails it has already handled, so the next
  `--apply` only looks at new mail. To start over, delete the file
  `.triage_state.json`.
- Preview runs always look at all unread mail, so you can run them as often as you like.

### Options

| Option | What it does | Default |
| --- | --- | --- |
| `--apply` | Add the suggested labels in Gmail | off |
| `--limit 20` | Check at most this many unread emails (newest first) | 50 |
| `--threshold 0.6` | How sure it must be to call an email important | 0.5 |
| `--label-threshold 0.8` | How sure it must be to suggest a label | 0.7 |

## Privacy

The sender, recipient, subject, date, and text of each unread email are sent
to TypeSafe to be sorted, along with the sender and subject of a few example
emails from each of your labels. Your keys stay on your computer in `.env`.
