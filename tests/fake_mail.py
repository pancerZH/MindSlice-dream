"""A stand-in for Gmail: SMTP_SSL delivers into one mailbox, and IMAP4_SSL serves that mailbox the way Gmail does
(an All Mail folder with a localized name, X-GM-LABELS in FETCH responses). X-GM-RAW searches return every message,
so the program's own checks are what keeps the wrong ones out."""

from email import message_from_bytes, policy
from email.message import EmailMessage

SENT = (b'"\\\\Sent"', b'"\\\\Inbox"')  # mail the account sent to itself


class Mailbox:
    def __init__(self):
        self.messages: list[tuple[tuple[bytes, ...], bytes]] = []
        self.logins = 0

    def deliver(self, msg: EmailMessage, labels: tuple[bytes, ...] = SENT) -> None:
        self.messages.append((labels, msg.as_bytes(policy=policy.SMTP)))

    def received(self) -> list[EmailMessage]:
        return [message_from_bytes(raw, policy=policy.default) for _, raw in self.messages]


class FakeSMTP:
    box: Mailbox

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        assert (user, password) == ("me@example.com", "app-password")

    def send_message(self, msg):
        self.box.deliver(msg)


class FakeIMAP:
    box: Mailbox
    ALL = "[Gmail]/&YkBnCZCuTvY-"

    def __init__(self, host, port=993, timeout=None):
        self.box.logins += 1

    def login(self, user, password):
        assert (user, password) == ("me@example.com", "app-password")
        return "OK", [b"logged in"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "%s"' % self.ALL.encode()]

    def select(self, name, readonly=False):
        assert name == f'"{self.ALL}"' and readonly
        return "OK", [str(len(self.box.messages)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            assert args[0] == "X-GM-RAW"
            return "OK", [" ".join(str(uid) for uid in range(1, len(self.box.messages) + 1)).encode()]
        assert command == "FETCH"
        uids, spec = args
        data = []
        for uid in map(int, uids.split(",")):
            labels, raw = self.box.messages[uid - 1]
            if "HEADER" in spec:
                head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                prefix = b"%d (X-GM-LABELS (%s) UID %d BODY[HEADER] {%d}" % (uid, b" ".join(labels), uid, len(head))
                data += [(prefix, head), b")"]
            else:
                data += [(b"%d (UID %d BODY[] {%d}" % (uid, uid, len(raw)), raw), b")"]
        return "OK", data

    def logout(self):
        return "BYE", [b"logging out"]
