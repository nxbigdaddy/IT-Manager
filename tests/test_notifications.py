"""All network boundaries are mocked; these tests never deliver notifications."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import ssl
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from itmanager import notifications as n


URLS = {
    'wecom': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TOKEN',
    'feishu': 'https://open.feishu.cn/open-apis/bot/v2/hook/TOKEN',
    'dingtalk': 'https://oapi.dingtalk.com/robot/send?access_token=TOKEN',
}


@pytest.fixture(autouse=True)
def network(monkeypatch):
    response = MagicMock()
    response.status = 200
    response.read.return_value = b'{"errcode":0,"code":0}'
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value = response
    build = MagicMock(return_value=opener)
    monkeypatch.setattr(n.request, 'build_opener', build)
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.send_message.return_value = {}
    ssl_factory = MagicMock(return_value=smtp)
    plain_factory = MagicMock(return_value=smtp)
    monkeypatch.setattr(n.smtplib, 'SMTP_SSL', ssl_factory)
    monkeypatch.setattr(n.smtplib, 'SMTP', plain_factory)
    return response, opener, build, smtp, ssl_factory, plain_factory


def webhook(monkeypatch, kind):
    monkeypatch.setenv('TEST_HOOK', URLS[kind])
    return {'type': kind, 'webhook_env': 'TEST_HOOK'}


def email():
    return {'type': 'email', 'host': 'smtp.example.com', 'from_addr': 'a@example.com',
            'to_addrs': ['b@example.com']}


@pytest.mark.parametrize('kind', URLS)
def test_webhook_success(monkeypatch, network, kind):
    assert n.send_notification(webhook(monkeypatch, kind), '标题', '正文') == {'ok': True, 'channel': kind}
    req = network[1].open.call_args.args[0]
    assert req.method == 'POST'
    payload = json.loads(req.data)
    assert '标题\n正文' == (payload.get('content') or payload['text'])['text' if kind == 'feishu' else 'content']
    assert network[1].open.call_args.kwargs['timeout'] == 10
    handlers = network[2].call_args.args
    assert any(isinstance(h, n._NoRedirect) for h in handlers)
    assert any(isinstance(h, n.request.ProxyHandler) and h.proxies == {} for h in handlers)


@pytest.mark.parametrize('url', [
    'http://qyapi.weixin.qq.com/cgi-bin/webhook/send',
    'https://qyapi.weixin.qq.com.evil.example/cgi-bin/webhook/send',
    'https://127.0.0.1/cgi-bin/webhook/send',
    'https://user:password@qyapi.weixin.qq.com/cgi-bin/webhook/send',
    'https://qyapi.weixin.qq.com:444/cgi-bin/webhook/send',
    'https://qyapi.weixin.qq.com/cgi-bin/webhook/send#TOKEN',
    'https://qyapi.weixin.qq.com/other',
    'https://qyapi.weixin.qq.com/cgi-bin/webhook/send\n',
])
def test_untrusted_webhook(monkeypatch, network, url):
    channel = webhook(monkeypatch, 'wecom')
    monkeypatch.setenv('TEST_HOOK', url)
    with pytest.raises(n.NotificationError):
        n.send_notification(channel, '', '')
    network[1].open.assert_not_called()


def test_redirect_denied():
    with pytest.raises(n.NotificationError):
        n._NoRedirect().redirect_request(None, None, 302, 'TOKEN', {}, 'http://127.0.0.1')


@pytest.mark.parametrize('kind', ['dingtalk', 'feishu'])
def test_signatures(monkeypatch, network, kind):
    channel = webhook(monkeypatch, kind)
    channel['secret_env'] = 'TEST_SECRET'
    monkeypatch.setenv('TEST_SECRET', 'signing-secret')
    monkeypatch.setattr(n.time, 'time', lambda: 1700000000)
    n.send_notification(channel, 's', 'b')
    req = network[1].open.call_args.args[0]
    if kind == 'dingtalk':
        query = parse_qs(urlsplit(req.full_url).query)
        expected = base64.b64encode(hmac.new(b'signing-secret', b'1700000000000\nsigning-secret', hashlib.sha256).digest()).decode()
        assert query['timestamp'] == ['1700000000000']
        assert query['sign'] == [expected]
        assert query['access_token'] == ['TOKEN']
    else:
        payload = json.loads(req.data)
        expected = base64.b64encode(hmac.new(b'1700000000\nsigning-secret', b'', hashlib.sha256).digest()).decode()
        assert payload['timestamp'] == '1700000000'
        assert payload['sign'] == expected


@pytest.mark.parametrize('raw', [b'{}', b'[]', b'not json', b'{"errcode":1,"errmsg":"TOKEN"}', b'{"errcode":false}', b'{"errcode":"0"}', b'x' * (n.MAX_RESPONSE_BYTES + 1)], ids=['missing', 'array', 'invalid-json', 'rejected', 'bool', 'string', 'oversized'])
def test_bad_response_safe(monkeypatch, network, raw):
    network[0].read.return_value = raw
    with pytest.raises(n.NotificationError) as exc:
        n.send_notification(webhook(monkeypatch, 'wecom'), '', '')
    assert 'TOKEN' not in str(exc.value)


@pytest.mark.parametrize('raw', [b'{"StatusCode":0}', b'{"code":0}'])
def test_feishu_response_variants(monkeypatch, network, raw):
    network[0].read.return_value = raw
    assert n.send_notification(webhook(monkeypatch, 'feishu'), '', '')['ok']


def test_network_error_sanitized(monkeypatch, network):
    network[1].open.side_effect = OSError('https://secret.example/?TOKEN=password')
    with pytest.raises(n.NotificationError) as exc:
        n.send_notification(webhook(monkeypatch, 'wecom'), '', '')
    assert 'TOKEN' not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize('timeout', [0, -1, 61, True, '10', float('nan')])
def test_timeout_invalid(monkeypatch, network, timeout):
    channel = webhook(monkeypatch, 'wecom')
    channel['timeout'] = timeout
    with pytest.raises(n.NotificationError):
        n.send_notification(channel, '', '')
    network[1].open.assert_not_called()


def test_config_and_webhook_attachments(monkeypatch, network):
    channel = webhook(monkeypatch, 'wecom')
    with pytest.raises(n.NotificationError):
        n.send_notification(channel, '', '', ['file.txt'])
    with pytest.raises(n.NotificationError):
        n.send_notification({**channel, 'secret': 'TOKEN'}, '', '')
    monkeypatch.delenv('TEST_HOOK')
    with pytest.raises(n.NotificationError):
        n.send_notification(channel, '', '')
    network[1].open.assert_not_called()


@pytest.mark.parametrize('security', ['ssl', 'starttls'])
def test_email_tls_and_attachment(monkeypatch, network, tmp_path, security):
    channel = {**email(), 'security': security, 'username_env': 'MAIL_USER', 'password_env': 'MAIL_PASS'}
    monkeypatch.setenv('MAIL_USER', 'user')
    monkeypatch.setenv('MAIL_PASS', 'TOKEN')
    path = tmp_path / 'report.txt'
    path.write_text('attachment data', encoding='utf-8')
    assert n.send_notification(channel, 'report', 'body', [str(path)])['ok']
    smtp = network[3]
    factory = network[4] if security == 'ssl' else network[5]
    assert factory.call_args.kwargs['timeout'] == 10
    context = factory.call_args.kwargs['context'] if security == 'ssl' else smtp.starttls.call_args.kwargs['context']
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    names = [call[0] for call in smtp.mock_calls]
    if security == 'starttls':
        assert names.index('starttls') < names.index('login') < names.index('send_message')
    else:
        smtp.starttls.assert_not_called()
    msg = smtp.send_message.call_args.args[0]
    attachment = list(msg.iter_attachments())[0]
    assert attachment.get_filename() == 'report.txt'
    assert attachment.get_payload(decode=True) == b'attachment data'


def test_email_defaults_verified_ssl(network):
    n.send_notification(email(), 's', 'b')
    assert network[4].call_args.args == ('smtp.example.com', 465)
    network[3].login.assert_not_called()
    network[5].assert_not_called()


@pytest.mark.parametrize('security', ['none', 'plain', '', None])
def test_email_plaintext_forbidden(network, security):
    with pytest.raises(n.NotificationError):
        n.send_notification({**email(), 'security': security}, '', '')
    network[4].assert_not_called()
    network[5].assert_not_called()


def test_starttls_failure_never_logs_in(monkeypatch, network):
    monkeypatch.setenv('U', 'user')
    monkeypatch.setenv('P', 'TOKEN')
    network[3].starttls.side_effect = OSError('TOKEN')
    with pytest.raises(n.NotificationError, match='^Notification delivery failed'):
        n.send_notification({**email(), 'security': 'starttls', 'username_env': 'U', 'password_env': 'P'}, '', '')
    network[3].login.assert_not_called()
    network[3].send_message.assert_not_called()


def test_refused_recipient_safe(network):
    network[3].send_message.return_value = {'private@example.com': (550, b'TOKEN')}
    with pytest.raises(n.NotificationError) as exc:
        n.send_notification(email(), '', '')
    assert 'private' not in str(exc.value) and 'TOKEN' not in str(exc.value)


def test_attachment_limits(monkeypatch, network, tmp_path):
    monkeypatch.setattr(n, 'MAX_ATTACHMENT_BYTES', 4)
    monkeypatch.setattr(n, 'MAX_TOTAL_ATTACHMENT_BYTES', 6)
    path = tmp_path / 'a.txt'
    path.write_bytes(b'12345')
    with pytest.raises(n.NotificationError, match='size limit'):
        n.send_notification(email(), '', '', [str(path)])
    path.write_bytes(b'1234')
    with pytest.raises(n.NotificationError, match='size limit'):
        n.send_notification(email(), '', '', [str(path), str(path)])
    with pytest.raises(n.NotificationError, match='Too many'):
        n.send_notification(email(), '', '', [str(path)] * 21)
    with pytest.raises(n.NotificationError, match='Unable to read'):
        n.send_notification(email(), '', '', [str(tmp_path / 'missing')])
    network[4].assert_not_called()


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize('schedule,after,expected', [
    ({'frequency': 'daily', 'hour': 9, 'minute': 30}, '2024-01-01T09:29:59', '2024-01-01T09:30:00'),
    ({'frequency': 'daily', 'hour': 9}, '2024-01-01T09:00:00', '2024-01-02T09:00:00'),
    ({'type': 'weekly', 'weekday': 0, 'hour': 9}, '2024-01-01T09:00:00', '2024-01-08T09:00:00'),
    ({'frequency': 'monthly', 'day': 31}, '2024-02-01T00:00:00', '2024-02-29T00:00:00'),
    ({'frequency': 'monthly', 'day': 31}, '2023-02-01T00:00:00', '2023-02-28T00:00:00'),
    ({'frequency': 'monthly', 'day': 31}, '2024-12-31T00:00:00', '2025-01-31T00:00:00'),
    ({'frequency': 'daily', 'hour': 9, 'timezone': 'Asia/Shanghai'}, '2024-01-01T00:00:00', '2024-01-01T01:00:00'),
    ({'frequency': 'daily', 'hour': 2, 'minute': 30, 'timezone': 'America/New_York'}, '2024-03-10T00:00:00', '2024-03-10T07:30:00'),
    ({'frequency': 'daily', 'hour': 1, 'minute': 30, 'timezone': 'America/New_York'}, '2024-11-03T05:30:00', '2024-11-04T06:30:00'),
])
def test_schedules(schedule, after, expected):
    assert n.next_run(schedule, utc(after)) == utc(expected)
    assert n.next_run(schedule, utc(after)).tzinfo is timezone.utc


@pytest.mark.parametrize('extra', [{'frequency': 'hourly'}, {'hour': 24}, {'minute': -1}, {'weekday': 7}, {'day': 0}, {'day': 32}, {'hour': True}, {'timezone': 'Invalid/Zone'}])
def test_invalid_schedules(extra):
    with pytest.raises(n.NotificationError):
        n.next_run({'frequency': 'daily', **extra}, utc('2024-01-01T00:00:00'))


def test_naive_schedule_rejected():
    with pytest.raises(n.NotificationError):
        n.next_run({'frequency': 'daily'}, datetime(2024, 1, 1))
