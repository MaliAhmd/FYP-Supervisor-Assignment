"""Transport contracts and SMTP delivery; never called by an agent."""
from dataclasses import dataclass
from email.message import EmailMessage
import smtplib
import ssl
from typing import Protocol


class DeliveryError(RuntimeError):
    def __init__(self, code='delivery_failed', *, uncertain=True):
        allowed = {'delivery_failed', 'smtp_connect_failed', 'smtp_rejected', 'gmail_rejected', 'gmail_failed'}
        super().__init__(code if code in allowed else 'delivery_failed')
        self.uncertain = uncertain


@dataclass(frozen=True)
class Receipt:
    provider_id: str | None = None


class EmailSender(Protocol):
    def send(self, message: EmailMessage, *, recipient: str) -> Receipt: ...


class SmtpSender:
    def __init__(self, settings, *, smtp_factory=None, ssl_factory=None):
        self.settings = settings
        self.smtp_factory = smtp_factory or smtplib.SMTP
        self.ssl_factory = ssl_factory or smtplib.SMTP_SSL

    def send(self, message, *, recipient):
        smtp, submitting = None, False
        try:
            cfg = self.settings
            context = ssl.create_default_context()
            if cfg.smtp_tls_mode == 'ssl':
                smtp = self.ssl_factory(cfg.smtp_host, cfg.smtp_port, timeout=cfg.request_timeout_seconds, context=context)
            else:
                smtp = self.smtp_factory(cfg.smtp_host, cfg.smtp_port, timeout=cfg.request_timeout_seconds)
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            if cfg.smtp_username:
                smtp.login(cfg.smtp_username, cfg.smtp_password.get_secret_value())
            submitting = True
            refused = smtp.send_message(message, from_addr=cfg.smtp_from, to_addrs=[recipient])
            if refused:
                raise DeliveryError('smtp_rejected', uncertain=False)
            return Receipt()
        except DeliveryError:
            raise
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise DeliveryError('smtp_rejected', uncertain=False) from None
        except Exception:
            raise DeliveryError('delivery_failed' if submitting else 'smtp_connect_failed', uncertain=submitting) from None
        finally:
            if smtp:
                # An error closing a successfully accepted message is not a failed send.
                try:
                    smtp.close()
                except Exception:
                    pass
