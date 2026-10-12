from unittest.mock import patch
from fastapi.testclient import TestClient
from itmanager.server import create_app
from itmanager.ai_consent import records_hash

def test_form_context_changed_after_preview_never_calls_ai(tmp_path):
    app=create_app(tmp_path,'t'*32)
    store=app.state.store
    record=store.save_record('tasks',{'名称':'授权时资料'})
    provider=store.put('providers',{'provider':'deepseek','model':'deepseek-chat','enabled':True})
    prompt=next(p for p in store.objects('prompts') if p.get('module')=='tasks')
    payload={'module':'tasks','basis':'安排检查','current':{},'context_ids':[record['id']],'context_hash':records_hash([record]),'provider_id':provider['id'],'prompt_id':prompt['id'],'confirmed':True}
    store.save_record('tasks',{'名称':'未授权的新敏感资料'},record['id'],record['version'])
    with TestClient(app,headers={'Authorization':'Bearer '+'t'*32}) as client,patch('itmanager.ai.generate') as generate:
        response=client.post('/ai/form-fill',json=payload)
        assert response.status_code==400
        assert '资料已变化' in response.text
        generate.assert_not_called()
        assert client.post('/ai/generate',json={'record_ids':[{}]}).status_code==400
