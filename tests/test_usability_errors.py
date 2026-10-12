import io
import json
import urllib.error
from unittest.mock import patch
import pytest
from itmanager.client import APIClient,ClientError
from itmanager.errors import user_message

@pytest.mark.parametrize('message',['完成任务需要完成证据和验收人','截止日期应为有效的YYYY-MM-DD','考核定稿缺少: 人员ID、复核人'])
def test_clear_business_errors(message):
    assert user_message(message)==message
    body=io.BytesIO(json.dumps({'user_message':message}).encode())
    error=urllib.error.HTTPError('http://localhost',400,'bad',{},body)
    with patch('urllib.request.OpenerDirector.open',side_effect=error):
        with pytest.raises(ClientError,match=message):APIClient('http://localhost','x'*32).get('/records')

@pytest.mark.parametrize('message',['token secret123','考核定稿缺少: secret-token',None,{'unexpected':'dict'}])
def test_unknown_message_not_exposed(message):
    if isinstance(message,dict):
        body=io.BytesIO(json.dumps({'user_message':message}).encode())
        error=urllib.error.HTTPError('http://localhost',400,'bad',{},body)
        with patch('urllib.request.OpenerDirector.open',side_effect=error):
            with pytest.raises(ClientError,match='数据校验失败'):APIClient('http://localhost','x'*32).get('/records')
    else:assert user_message(message) is None
