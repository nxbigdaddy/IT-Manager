from fastapi.testclient import TestClient
from itmanager.server import create_app

def test_task_checklist_and_workspace_end_to_end(tmp_path):
    app=create_app(tmp_path,'x'*32)
    with TestClient(app,headers={'Authorization':'Bearer '+'x'*32}) as c:
        templates=c.get('/workspace-templates').json();assert len(templates)==3
        body={'template_id':templates[0]['id'],'name':'上市IT专项','owner':'张经理','start_date':'2030-01-01','due_date':'2030-06-30','request_id':'workspace_route_test_001'}
        r=c.post('/workspaces',json=body);assert r.status_code==200,r.text
        ws=r.json();assert c.post('/workspaces',json=body).json()['id']==ws['id']
        related=c.get('/workspaces/'+ws['id']+'/records').json()
        task=next(row for row in related if row['module']=='tasks')
        p={'action':'progress','version':task['version'],'payload':{'next_step':'补齐审计资料'},'request_id':'task_action_route_001'}
        action=c.post('/tasks/'+task['id']+'/action',json=p);assert action.status_code==200,action.text
        assert c.post('/tasks/'+task['id']+'/action',json=p).json()==action.json()
        ids=[row['id'] for row in c.get('/workspaces/'+ws['id']+'/records').json()]
        assert action.json()['log']['id'] in ids
        check={'name':'备份巡检','date':'2030-01-02','owner':'张经理','items':[{'name':'离线恢复抽检','result':'异常','note':'未找到最近恢复记录'}],'request_id':'checklist_route_test_001'}
        r=c.post('/checklists/complete',json=check);assert r.status_code==200,r.text
        result=r.json();assert len(result['tasks'])==len(result['risks'])==1
        assert c.get('/checklists/runs/'+result['run']['id']).status_code==200
        task=result['tasks'][0]
        completed=c.post('/tasks/'+task['id']+'/action',json={'action':'complete','version':task['version'],'payload':{'evidence':'受控恢复测试记录编号REC-1','acceptor':'张经理'},'request_id':'complete_check_task_001'})
        assert completed.status_code==200,completed.text
        report=c.post('/workspaces/'+ws['id']+'/report',json={});assert report.status_code==200,report.text
        assert report.json()['status']=='draft'
        prompt=ws['template_snapshot']['prompts'][0]['name']
        preview=c.get('/workspaces/'+ws['id']+'/ai-preview',params={'prompt_name':prompt}).json()
        assert len(preview['context_hash'])==64
        provider=c.post('/objects/providers',json={'data':{'provider':'deepseek','model':'deepseek-chat','api_key_env':'DEEPSEEK_API_KEY'}}).json()
        stale=c.post('/workspaces/'+ws['id']+'/ai',json={'provider_id':provider['id'],'prompt_name':prompt,'confirmed':True,'sensitive_confirmed':True,'context_hash':'stale'})
        assert stale.status_code==400 and '已变化' in stale.text
        assert '未找到最近恢复记录' not in report.json()['content']

def test_workspace_ai_requires_two_authorizations(tmp_path):
    app=create_app(tmp_path,'x'*32)
    with TestClient(app,headers={'Authorization':'Bearer '+'x'*32}) as c:
        r=c.post('/workspaces/none/ai',json={'confirmed':True})
        assert r.status_code==400
        assert '二次' in r.text
