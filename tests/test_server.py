import tempfile
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from itmanager.server import create_app

TOKEN='test-admin-token-not-a-real-secret-1234'

@pytest.fixture
def app_client(tmp_path):
    app=create_app(tmp_path,TOKEN)
    with TestClient(app,headers={'Authorization':'Bearer '+TOKEN}) as client:
        yield app,client


def test_empty_report_module_selection_excludes_all(app_client):
    app,c=app_client
    app.state.store.save_record('tasks',{'名称':'不得意外包含'})
    today=datetime.now(timezone.utc).date().isoformat()
    response=c.post('/reports',json={'start':today,'end':today,'modules':[]})
    assert response.status_code==200
    assert '不得意外包含' not in response.json()['content']


def test_auth(app_client):
    app,c=app_client
    assert c.get('/health').status_code==200
    assert c.get('/health',headers={'Authorization':'bad'}).status_code==401


def test_closed_loop(app_client):
    app,c=app_client
    def save(module,data):
        r=c.post('/records/'+module,json={'data':data});assert r.status_code==200,r.text;return r.json()
    person=save('people',{'姓名':'测试工程师','部门':'IT'})
    plan=save('plans',{'名称':'年度可靠性提升','负责人':'测试工程师'})
    project=save('projects',{'名称':'恢复能力建设','规划ID':plan['id']})
    task=save('tasks',{'名称':'隔离恢复验证','项目ID':project['id'],'负责人':'测试工程师','状态':'进行中','截止日期':'2030-01-01'})
    save('logs',{'名称':'恢复演练记录','人员ID':person['id'],'关联ID':task['id'],'结果':'发现依赖缺口'})
    risk=save('risks',{'名称':'恢复依赖不完整','关联ID':task['id'],'等级':'高','负责人':'测试工程师'})
    save('decisions',{'名称':'补齐恢复资源','来源ID':risk['id'],'结论':'批准隔离演练资源'})
    task['data'].update({'状态':'已完成','完成证据':'受控演练报告','验收人':'业务主管'})
    result=c.post('/records/tasks',json={'id':task['id'],'version':task['version'],'data':task['data']})
    assert result.status_code==200,result.text
    assert c.post('/records/tasks',json={'id':task['id'],'version':task['version'],'data':task['data']}).status_code==400
    save('reviews',{'名称':'季度考核','人员ID':person['id'],'考核周期':'2030Q1','成果证据':task['id'],'主管评价':'按实际证据评估','员工反馈':'已阅'})
    today=datetime.now(timezone.utc).date().isoformat()
    report=c.post('/reports',json={'start':today,'end':today}).json()
    assert task['id'] in report['content']
    channel=c.post('/objects/channels',json={'data':{'name':'测试群','type':'wecom','webhook_env':'ITM_SECRET_TEST_WEBHOOK'}}).json()
    assert c.post('/send',json={'draft_id':report['id'],'channel_id':channel['id'],'request_id':'first-request-123456789'}).status_code==400
    approved=c.post('/drafts/'+report['id']+'/review',json={'approved':True,'revision':1}).json()
    with patch('itmanager.notifications.send_notification',return_value={'ok':True}) as send:
        payload={'draft_id':report['id'],'channel_id':channel['id'],'request_id':'first-request-123456789','draft_revision':approved['revision']}
        assert c.post('/send',json=payload).json()['state']=='sent'
        assert c.post('/send',json=payload).json()['state']=='duplicate'
        assert send.call_count==1
    exported=c.get('/export').json()
    assert len(exported['records'])==8
    backup=c.post('/backup').json()['filename']
    assert (Path(app.state.store.path).parent/'backups'/backup).is_file()
    assert c.get('/audit').json()


def test_ai_and_prompt_version(app_client):
    app,c=app_client
    p=c.post('/objects/providers',json={'data':{'provider':'deepseek','model':'deepseek-chat','api_key_env':'DEEPSEEK_API_KEY'}}).json()
    template=c.get('/objects/prompts').json()[0]
    old_version=template['version']
    r=c.post('/objects/prompts',json={'id':template['id'],'data':dict(template,template='材料：{{content}}')})
    assert r.status_code==200,r.text
    assert r.json()['version']==old_version+1
    assert c.get('/prompt-history').json()[0]['prompt_id']==template['id']
    with patch('itmanager.ai.generate',return_value={'text':'AI审核草稿','usage':{},'model':'mock'}) as generate:
        result=c.post('/ai/generate',json={'provider_id':p['id'],'prompt_id':template['id'],'record_ids':[],'variables':{'content':'虚构会议'},'confirmed':True})
        assert result.status_code==200,result.text
        assert result.json()['status']=='draft'
        assert '虚构会议' in generate.call_args.args[2]
    assert c.get('/ai/calls').json()[0]['status']=='success'


def test_schedule_idempotency_and_uncertain(app_client):
    app,c=app_client
    channel=app.state.store.put('channels',{'name':'test','type':'wecom','webhook_env':'ITM_SECRET_X'})
    schedule={'name':'daily','kind':'report','mode':'auto_send','channel_id':channel['id'],'enabled':True,'period_days':7,'schedule':{'frequency':'daily','hour':9,'minute':0,'timezone':'UTC'}}
    r=c.post('/objects/schedules',json={'data':schedule});assert r.status_code==200,r.text
    job=r.json();job['next_run']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    app.state.store.put('schedules',job,job['id'])
    with patch('itmanager.notifications.send_notification',side_effect=TimeoutError) as send:
        app.state.tick();app.state.tick()
        assert send.call_count==1
    assert c.get('/deliveries').json()[0]['state']=='uncertain'


def test_sensitive_ai_requires_confirmation(app_client):
    app,c=app_client
    person=c.post('/records/people',json={'data':{'姓名':'虚构人员'}}).json()
    provider=c.post('/objects/providers',json={'data':{'provider':'qwen','model':'qwen-plus','api_key_env':'DASHSCOPE_API_KEY'}}).json()
    prompt=c.get('/objects/prompts').json()[0]
    response=c.post('/ai/generate',json={'provider_id':provider['id'],'prompt_id':prompt['id'],'record_ids':[person['id']],'confirmed':True})
    assert response.status_code==400
    assert '二次授权' in response.text


def test_manual_draft_edit_invalidates_approval(app_client):
    _,c=app_client
    d=c.post('/drafts',json={'title':'任务下达','content':'请完成核验'}).json()
    d=c.post('/drafts/'+d['id']+'/review',json={'approved':True,'revision':1}).json()
    assert d['status']=='approved'
    d=c.post('/drafts/'+d['id']+'/review',json={'content':'修改后的任务','approved':False,'revision':d['revision']}).json()
    assert d['status']=='draft' and d['approved_revision'] is None


def test_disabled_channel_blocks_scheduled_send(app_client):
    app,c=app_client
    channel=app.state.store.put('channels',{'name':'disabled','type':'wecom','webhook_env':'ITM_SECRET_X','enabled':False})
    app.state.store.put('schedules',{'name':'blocked','kind':'report','mode':'auto_send','channel_id':channel['id'],'enabled':True,'period_days':7,'schedule':{'frequency':'daily','hour':9,'minute':0,'timezone':'UTC'},'next_run':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()})
    with patch('itmanager.notifications.send_notification') as send:
        app.state.tick()
        send.assert_not_called()
    assert c.get('/deliveries').json()[0]['state']=='blocked'


def test_raw_credentials_rejected(app_client):
    _,c=app_client
    assert c.post('/objects/providers',json={'data':{'provider':'kimi','model':'x','api_key':'do-not-store'}}).status_code==400
