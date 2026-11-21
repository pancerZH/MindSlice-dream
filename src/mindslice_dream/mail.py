"""Mail for the review: SMTP and IMAP with news-agent's Gmail account file, Gmail search, and which replies count."""

import html
import imaplib
import re
import smtplib
import tomllib
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path

MSGID_DOMAIN = "mindslice-dream"  # make_msgid() would put this machine's hostname into every mail's headers
SUBJECT_TAG = "MindSlice dream"
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def account(cfg: dict) -> dict:
    """host, port, username, password: news-agent's smtp.toml (its `to` list is not used)."""
    return tomllib.loads(Path(cfg["mail"]["account"]).expanduser().read_text(encoding="utf-8"))


def ids(*headers) -> list[str]:
    """The message ids in header values such as References and In-Reply-To."""
    return [i for h in headers for i in re.findall(r"<[^<>\s]+>", str(h or ""))]


def ours(message_id: str) -> bool:
    return str(message_id or "").strip().endswith(f"@{MSGID_DOMAIN}>")


def mailbox(address) -> str:
    """'Name <a+dream@gmail.com>' -> 'a@gmail.com': the account a (plus) address delivers to."""
    local, _, domain = parseaddr(str(address or ""))[1].lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}"


def compose(cfg: dict, subject: str, text: str, page: str | None = None, reply_to=None,
            attachment: tuple[str, str] | None = None) -> EmailMessage:
    """A mail to the configured address; with reply_to, an answer in that mail's thread; attachment: (file name, text)."""
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, account(cfg)["username"], cfg["mail"]["to"]
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = make_msgid(domain=MSGID_DOMAIN)
    if reply_to is not None:
        parent = ids(reply_to["Message-ID"])
        msg["In-Reply-To"] = " ".join(parent)
        msg["References"] = " ".join((ids(reply_to["References"]) or ids(reply_to["In-Reply-To"])) + parent)
    msg.set_content(text)
    if page:
        msg.add_alternative(page, subtype="html")
    if attachment:
        msg.add_attachment(attachment[1], filename=attachment[0])
    return msg


def send(cfg: dict, msg: EmailMessage) -> None:
    acc = account(cfg)
    port = acc.get("port", 465)
    with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(acc["host"], port, timeout=30) as smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(acc["username"], acc["password"])
        smtp.send_message(msg)


def labels(fetch_prefix: bytes) -> set[str]:
    """The Gmail labels in a FETCH response's X-GM-LABELS list, unquoted: {'\\Inbox', '\\Sent', 'My label'}."""
    found = set()
    for m in re.finditer(rb'"((?:[^"\\]|\\.)*)"|([^\s()"]+)|\)', fetch_prefix.partition(b"X-GM-LABELS (")[2]):
        if m.group(0) == b")":
            break
        word = re.sub(rb"\\(.)", rb"\1", m.group(1)) if m.group(1) is not None else m.group(2)
        found.add(word.decode(errors="replace"))
    return found


def all_mail(imap) -> str:
    """Gmail's All Mail folder, found by its \\All attribute: its name follows the account's interface language."""
    _, folders = imap.list()
    line = next((f for f in folders if b"\\All" in f.split(b")", 1)[0]), None)
    if line is None:
        raise RuntimeError("no folder with the \\All attribute: is this a Gmail account?")
    m = re.search(rb'(?:"((?:[^"\\]|\\.)*)"|(\S+))\s*$', line)
    return f'"{(m.group(1) or m.group(2)).decode()}"'


def fetch(cfg: dict, days: int, skip: set[str]) -> list[EmailMessage]:
    """The review threads' recent mails that count as the user's replies, oldest first: labelled \\Sent (only someone
    logged in to the account can send them), from and to the configured mailbox (a forward to someone else does not
    count), and neither the program's own mails nor those in skip."""
    acc, mine = account(cfg), mailbox(cfg["mail"]["to"])
    imap = imaplib.IMAP4_SSL(cfg["mail"]["imap"], timeout=60)
    try:
        imap.login(acc["username"], acc["password"])
        imap.select(all_mail(imap), readonly=True)
        # Gmail's own search: a plain IMAP HEADER search times out on a large mailbox
        _, data = imap.uid("SEARCH", "X-GM-RAW", f'"in:sent subject:({SUBJECT_TAG}) newer_than:{days}d"')
        uids = data[0].split()
        if not uids:
            return []
        _, data = imap.uid("FETCH", b",".join(uids).decode(), "(X-GM-LABELS BODY.PEEK[HEADER])")
        wanted = []
        for part in data:
            if not isinstance(part, tuple):
                continue
            head = message_from_bytes(part[1], policy=policy.default)
            mid = str(head["Message-ID"] or "").strip()
            to_me = any(mailbox(a) == mine for _, a in getaddresses([str(head["To"] or ""), str(head["Cc"] or "")]))
            if ("\\Sent" in labels(part[0]) and mailbox(head["From"]) == mine and to_me
                    and not ours(mid) and mid not in skip):
                wanted.append(int(re.search(rb"UID (\d+)", part[0]).group(1)))
        replies = []
        for uid in sorted(wanted):
            _, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            replies.append(message_from_bytes(next(p[1] for p in data if isinstance(p, tuple)), policy=policy.default))
        return replies
    finally:
        imap.logout()


def reply_text(msg: EmailMessage) -> str:
    """What the user wrote: the quoted mail, the "On ... wrote:" line above it and the signature are dropped, and
    addresses are masked, since the text goes to the model that reads it."""
    part = msg.get_body(preferencelist=("plain", "html"))
    text = part.get_content() if part is not None else ""
    if part is not None and part.get_content_type() == "text/html":
        text = re.split(r'<div class="gmail_quote|<blockquote', text, maxsplit=1)[0]
        text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>|</(?:p|div)>", "\n", text, flags=re.I)))
    kept = []
    for line in text.splitlines():
        if line.rstrip() == "--":  # "-- " starts the signature
            break
        if line.lstrip().startswith(">"):
            while kept and not kept[-1].strip():
                kept.pop()
            if kept and kept[-1].rstrip().endswith((":", "：")):  # "On Tue, ... wrote:", "... 写道：" (maybe wrapped)
                while kept and kept[-1].strip():
                    kept.pop()
            continue
        kept.append(line)
    return ADDRESS.sub("<邮箱>", "\n".join(kept)).strip()
