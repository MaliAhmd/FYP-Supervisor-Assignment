"""The Gmail send-only adapter. Uses the separate gmail.send credential."""
import base64
import threading
from email.policy import SMTP

from app.integrations.smtp.sender import DeliveryError, Receipt


class GmailApiSender:
    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()

    def send(self, message, *, recipient):
        # Application builds a fresh message with exactly one To and no Cc/Bcc.
        if message.get_all('To') != [recipient] or message.get('Cc') or message.get('Bcc'):
            raise DeliveryError('gmail_rejected', uncertain=False)
        raw = base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode('ascii')
        try:
            with self._lock:
                result = self.service.users().messages().send(userId='me', body={'raw': raw}).execute(num_retries=0)
            if not isinstance(result.get('id'), str) or not result['id']:
                raise ValueError('Missing receipt')
            return Receipt(result['id'])
        except Exception as error:
            status = getattr(getattr(error, 'resp', None), 'status', None)
            rejected = status in {400, 401, 403, 404, 413, 429}
            raise DeliveryError('gmail_rejected' if rejected else 'gmail_failed', uncertain=not rejected) from None
