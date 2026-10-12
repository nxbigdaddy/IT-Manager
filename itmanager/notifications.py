"""Standard-library notifications and calendar scheduling; no sends on import.

Secrets (including complete webhook URLs) are read only from environment variable
names supplied in configuration. Successful sends return {"ok": True, "channel":
<type>}; failures raise NotificationError with a fixed, non-sensitive message.
Webhook attachments are unsupported; email attachments are bounded to 10 MiB each
and 20 MiB total (raw bytes). Timeout defaults to 10 seconds, maximum 60 seconds.

next_run uses schedule['frequency'] (or 'type'), hour/minute, timezone (default
UTC), weekday (Monday=0), and day. It returns a UTC datetime strictly after the
aware input. Monthly days clamp to the month end. DST gaps shift forward by the
gap; repeated local times use the first occurrence only.
"""
from __future__ import annotations

import base64
import calendar
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import smtplib
import ssl
import time
from urllib import parse, request
from zoneinfo import ZoneInfo


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 20
MAX_RESPONSE_BYTES = 64 * 1024
_HOSTS = {
    'wecom': {'qyapi.weixin.qq.com'},
    'feishu': {'open.feishu.cn', 'open.larksuite.com'},
    'dingtalk': {'oapi.dingtalk.com'},
}


class NotificationError(Exception):
    """Safe to display: never contains remote responses, URLs, or credentials."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NotificationError('Webhook redirects are not allowed.')


def _env(config, name, required=True):
    key = config.get(name)
    if key is None and not required:
        return None
    if not isinstance(key, str) or not key or not os.environ.get(key):
        raise NotificationError('Required environment configuration is missing.')
    return os.environ[key]


def _timeout(config):
    value = config.get('timeout', 10)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 60:
        raise NotificationError('Invalid notification timeout.')
    return value


def _webhook_url(channel, kind):
    value = _env(channel, 'webhook_env')
    try:
        url = parse.urlsplit(value)
        valid = (url.scheme == 'https' and url.hostname in _HOSTS[kind]
                 and url.port in (None, 443) and not url.username
                 and not url.password and not url.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value))
        paths = {'wecom': '/cgi-bin/webhook/send', 'dingtalk': '/robot/send'}
        valid = valid and (url.path == paths[kind] if kind in paths
                           else url.path.startswith('/open-apis/bot/v2/hook/')
                           and bool(url.path.removeprefix('/open-apis/bot/v2/hook/')))
        if not valid:
            raise ValueError
    except Exception:
        raise NotificationError('Invalid or untrusted webhook endpoint.') from None
    return value


def _send_webhook(channel, kind, subject, body, attachments):
    if attachments:
        raise NotificationError('Attachments are supported only for email.')
    url = _webhook_url(channel, kind)
    text = subject + '\n' + body if subject else body
    if kind == 'feishu':
        payload = {'msg_type': 'text', 'content': {'text': text}}
    else:
        payload = {'msgtype': 'text', 'text': {'content': text}}
    secret = _env(channel, 'secret_env', required=False)
    if secret and kind == 'wecom':
        raise NotificationError('Signing is not supported for this channel.')
    if secret and kind == 'dingtalk':
        timestamp = str(int(time.time() * 1000))
        signature = base64.b64encode(hmac.new(
            secret.encode(), (timestamp + '\n' + secret).encode(), hashlib.sha256
        ).digest()).decode('ascii')
        parts = parse.urlsplit(url)
        query = [(k, v) for k, v in parse.parse_qsl(parts.query, keep_blank_values=True)
                 if k not in ('timestamp', 'sign')]
        query.extend([('timestamp', timestamp), ('sign', signature)])
        url = parse.urlunsplit(parts._replace(query=parse.urlencode(query)))
    elif secret and kind == 'feishu':
        timestamp = str(int(time.time()))
        payload['timestamp'] = timestamp
        payload['sign'] = base64.b64encode(hmac.new(
            (timestamp + '\n' + secret).encode(), b'', hashlib.sha256
        ).digest()).decode('ascii')
    req = request.Request(url, data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST')
    opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                  request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(req, timeout=_timeout(channel)) as response:
        if response.status != 200:
            raise NotificationError('Webhook delivery failed.')
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise NotificationError('Invalid webhook response.')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise NotificationError('Invalid webhook response.')
    # Feishu v2 uses code; legacy responses use StatusCode. Require an explicit
    # numeric zero, never accept a missing field, false, or a string "0".
    fields = ('code', 'StatusCode') if kind == 'feishu' else ('errcode',)
    codes = [result[key] for key in fields if key in result]
    if not codes or any(type(code) is not int or code != 0 for code in codes):
        raise NotificationError('Webhook rejected the notification.')


def _address(value):
    # Bare mailbox addresses only; display-name parsing would introduce ambiguity.
    if (not isinstance(value, str) or value.count('@') != 1
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(c in value for c in '<>,;') or value.startswith('@') or value.endswith('@')):
        raise NotificationError('Invalid email address configuration.')
    return value


def _send_email(channel, subject, body, attachments):
    security = channel.get('security', 'ssl')
    if security not in ('ssl', 'starttls'):
        raise NotificationError('SMTP requires verified TLS.')
    host = channel.get('host')
    port = channel.get('port', 465 if security == 'ssl' else 587)
    if (not isinstance(host, str) or not host or any(c.isspace() or ord(c) < 32 for c in host)
            or type(port) is not int or not 1 <= port <= 65535):
        raise NotificationError('Invalid SMTP server configuration.')
    username = _env(channel, 'username_env', required=False)
    password = _env(channel, 'password_env', required=False)
    if (username is None) != (password is None):
        raise NotificationError('SMTP credentials must be configured together.')
    sender = _address(channel.get('from_addr'))
    recipients = channel.get('to_addrs')
    if isinstance(recipients, str):
        recipients = [recipients]
    if not isinstance(recipients, (list, tuple)) or not recipients:
        raise NotificationError('Email recipients are required.')
    recipients = [_address(value) for value in recipients]
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, ', '.join(recipients), subject
    msg['Date'] = formatdate(localtime=False)
    msg.set_content(body)
    total = 0
    if len(attachments) > MAX_ATTACHMENTS:
        raise NotificationError('Too many email attachments.')
    for filename in attachments:
        path = Path(filename)
        if not path.is_file():
            raise NotificationError('Unable to read email attachment.')
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES or total + size > MAX_TOTAL_ATTACHMENT_BYTES:
            raise NotificationError('Email attachment size limit exceeded.')
        with path.open('rb') as stream:
            data = stream.read(min(MAX_ATTACHMENT_BYTES, MAX_TOTAL_ATTACHMENT_BYTES - total) + 1)
        total += len(data)
        if len(data) > MAX_ATTACHMENT_BYTES or total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise NotificationError('Email attachment size limit exceeded.')
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        main, sub = mime.split('/', 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=path.name)
    context = ssl.create_default_context()
    if security == 'ssl':
        client = smtplib.SMTP_SSL(host, port, timeout=_timeout(channel), context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=_timeout(channel))
    with client as smtp:
        if security == 'starttls':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if username is not None:
            smtp.login(username, password)
        refused = smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
        if refused:
            raise NotificationError('SMTP rejected one or more recipients.')


def send_notification(channel: dict, subject: str, body: str,
                      attachments: list[str] | None = None) -> dict:
    """Send once (no retries); errors are safe to display, including transport errors.

    channel.type: wecom/feishu/dingtalk/email. Webhooks use webhook_env and
    optional secret_env. Email uses host, port, security=ssl|starttls,
    username_env/password_env (both optional), from_addr and to_addrs.
    Inline webhook/secret/username/password configuration is rejected.
    """
    try:
        if not isinstance(channel, dict) or not isinstance(subject, str) or not isinstance(body, str):
            raise NotificationError('Invalid notification arguments.')
        if any(key in channel for key in ('webhook', 'webhook_url', 'url', 'secret', 'username', 'password')):
            raise NotificationError('Secrets must use environment configuration.')
        kind = channel.get('type')
        if kind not in ('wecom', 'feishu', 'dingtalk', 'email'):
            raise NotificationError('Unsupported notification channel.')
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list) or any(not isinstance(p, str) for p in attachments):
            raise NotificationError('Invalid attachment configuration.')
        _timeout(channel)
        if kind == 'email':
            _send_email(channel, subject, body, attachments)
        else:
            _send_webhook(channel, kind, subject, body, attachments)
        return {'ok': True, 'channel': kind}
    except NotificationError:
        raise
    except Exception:
        raise NotificationError('Notification delivery failed; check configuration and connectivity.') from None


def next_run(schedule: dict, after: datetime) -> datetime:
    """Return the next daily/weekly/monthly occurrence as an aware UTC datetime."""
    try:
        if not isinstance(schedule, dict) or not isinstance(after, datetime) or after.utcoffset() is None:
            raise ValueError
        frequency = schedule.get('frequency', schedule.get('type'))
        if frequency not in ('daily', 'weekly', 'monthly'):
            raise ValueError
        hour, minute = schedule.get('hour', 0), schedule.get('minute', 0)
        weekday, day = schedule.get('weekday', 0), schedule.get('day', 1)
        for value, low, high in ((hour, 0, 23), (minute, 0, 59), (weekday, 0, 6), (day, 1, 31)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError
        zone = ZoneInfo(schedule.get('timezone', 'UTC'))
        after_utc = after.astimezone(timezone.utc)
        date = after_utc.astimezone(zone).date()
        # A daily scan is simple, bounded, and handles all calendar transitions.
        for offset in range(370):
            current = date + timedelta(days=offset)
            if frequency == 'weekly' and current.weekday() != weekday:
                continue
            if frequency == 'monthly' and current.day != min(day, calendar.monthrange(current.year, current.month)[1]):
                continue
            local = datetime(current.year, current.month, current.day, hour, minute, tzinfo=zone, fold=0)
            candidate = local.astimezone(timezone.utc)
            if candidate > after_utc:
                return candidate
        raise ValueError
    except Exception:
        raise NotificationError('Invalid schedule or timezone configuration.') from None
