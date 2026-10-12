import json
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.schema import MODULES

@pytest.fixture
def setup(tmp_path):
    app=create_app(tmp_path,'t'*32)
    with TestClient(app,headers={'Authorization':'Bearer '+'t'*32}) as c:
        provider=c.post('/objects/providers',json={'data':{'provider':'deepseek','model':'deepseek-chat','api_key_env':'DEEPSEEK_API_KEY'}}).json()
        yield app,c,provider


def test_seed_templates_idempotent(setup):
    app,c,p=setup
    templates=[t for t in c.get('/objects/prompts').json() if t.get('purpose')=='form_fill']
    assert {t['module'] for t in templates}==set(MODULES)
    before=len(app.state.store.objects('prompts'))
    create_app(str(__import__('pathlib').Path(app.state.store.path).parent),'t'*32)
    assert len(app.state.store.objects('prompts'))==before


def payload(c,p,module='tasks'):
    prompt=next(t for t in c.get('/objects/prompts').json() if t.get('purpose')=='form_fill' and t.get('module')==module)
    return {'provider_id':p['id'],'prompt_id':prompt['id'],'module':module,'basis':'下月底完成电脑资产盘点','current':{},'context_ids':[],'confirmed':True}


def test_generate_without_saving_records(setup):
    app,c,p=setup
    response={'text':json.dumps({'fields':{'名称':'电脑资产盘点','下一步':'确认范围与盘点标准'},'suggestions':['建立核验检查点'],'warnings':[]},ensure_ascii=False),'model':'mock','usage':{}}
    with patch('itmanager.ai.generate',return_value=response):
        r=c.post('/ai/form-fill',json=payload(c,p))
    assert r.status_code==200,r.text
    assert r.json()['fields']['名称']=='电脑资产盘点'
    assert app.state.store.list_records()==[]
    assert app.state.store.objects('ai_calls')[-1]['purpose']=='form_fill'


def test_consent_and_sensitive_module(setup):
    _,c,p=setup
    body=payload(c,p,'reviews')
    assert c.post('/ai/form-fill',json=body).status_code==400
    body['confirmed']='true'
    assert c.post('/ai/form-fill',json=body).status_code==400


def test_prompt_defaults_and_versions(setup):
    _,c,p=setup
    body=payload(c,p)
    old=next(t for t in c.get('/objects/prompts').json() if t['id']==body['prompt_id'])
    new=dict(old);new.pop('id');new['name']='自定义任务填表'
    response=c.post('/objects/prompts',json={'data':new})
    assert response.status_code==200,response.text
    selected=[x for x in c.get('/objects/prompts').json() if x.get('module')=='tasks' and x.get('is_default')]
    assert len(selected)==1 and selected[0]['name']=='自定义任务填表'
    data=dict(selected[0]);data['template']+='\n请优先考虑资源限制。'
    edited=c.post('/objects/prompts',json={'id':data['id'],'data':data})
    assert edited.status_code==200
    assert edited.json()['version']==2
    assert c.get('/prompt-history').json()
