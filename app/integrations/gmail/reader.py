"""GET-only Gmail reader and bounded MIME decoding. No mailbox mutation methods."""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
import re
import threading

from app.config import validate_email


class MailReadError(RuntimeError):
    def __init__(self, code='gmail_read_failed'):
        allowed = {'gmail_read_failed', 'gmail_message_too_large', 'gmail_invalid_message', 'gmail_no_text', 'gmail_rate_limited'}
        super().__init__(code if code in allowed else 'gmail_read_failed')


@dataclass(frozen=True)
class InboundMessage:
    gmail_id: str
    sender: str
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    received_at: str
    body: str
    thread_id: str | None = None


def reference_ids(value):
    return tuple(dict.fromkeys(re.findall(r'<[^<>\s@]+@[^<>\s@]+>', value or '')))[:100]


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'p', 'br', 'div', 'li'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def decode_message(record, settings):
    try:
        raw = record['raw']
        if not isinstance(raw, str) or len(raw) > (settings.email_max_bytes + 2) // 3 * 4:
            raise MailReadError('gmail_message_too_large')
        data = base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True)
        if len(data) > settings.email_max_bytes:
            raise MailReadError('gmail_message_too_large')
        msg = BytesParser(policy=policy.default).parsebytes(data)
        if msg.defects or len(msg.get_all('From', [])) != 1:
            raise ValueError()
        addresses = getaddresses(msg.get_all('From', []))
        if len(addresses) != 1:
            raise ValueError()
        sender = validate_email(addresses[0][1])
        for header in ('Message-ID', 'In-Reply-To', 'References'):
            if len(msg.get_all(header, [])) > 1:
                raise ValueError()
        # get_body avoids attachments and picks one alternative, not duplicate HTML/text.
        part = msg.get_body(preferencelist=('plain', 'html'))
        if part is None or part.get_content_maintype() != 'text':
            raise MailReadError('gmail_no_text')
        body = part.get_content()
        if part.get_content_subtype() == 'html':
            parser = _Text()
            parser.feed(body)
            body = ''.join(parser.parts)
        body = ''.join(c for c in body if c in '\n\t' or ord(c) >= 32).strip()
        if not body:
            raise MailReadError('gmail_no_text')
        if len(body) > settings.email_max_body_chars:
            raise MailReadError('gmail_message_too_large')
        received = datetime.fromtimestamp(int(record['internalDate']) / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')
        ids = reference_ids(str(msg.get('Message-ID', '')))
        reply = reference_ids(str(msg.get('In-Reply-To', '')))
        if len(reply) > 1 or len(ids) > 1:
            raise ValueError()
        key = record['id']
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', key):
            raise ValueError()
        return InboundMessage(key, sender, ids[0] if ids else None, reply[0] if reply else None,
                              reference_ids(str(msg.get('References', ''))), received, body, record.get('threadId'))
    except MailReadError:
        raise
    except Exception:
        raise MailReadError('gmail_invalid_message') from None


class GmailReader:
    def __init__(self, service, settings):
        self.service, self.settings = service, settings
        self._lock = threading.Lock()

    def list_page(self, *, query, page_token=None):
        args = {'userId': 'me', 'q': query, 'maxResults': 100, 'includeSpamTrash': False}
        if page_token:
            args['pageToken'] = page_token
        try:
            with self._lock:
                result = self.service.users().messages().list(**args).execute(num_retries=0)
            ids = [item['id'] for item in result.get('messages', [])]
            if len(ids) > 100 or any(not isinstance(key, str) for key in ids):
                raise ValueError()
            token = result.get('nextPageToken')
            if token is not None and (not isinstance(token, str) or len(token) > 4096):
                raise ValueError()
            return ids, token
        except Exception:
            raise MailReadError() from None

    def read(self, gmail_id):
        try:
            with self._lock:
                record = self.service.users().messages().get(userId='me', id=gmail_id, format='raw').execute(num_retries=0)
        except Exception:
            raise MailReadError() from None
        if record.get('id') != gmail_id:
            raise MailReadError('gmail_invalid_message')
        return decode_message(record, self.settings)
