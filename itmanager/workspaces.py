"""Configuration-only workspaces. No script, URL fetching or AI calls are executed.

Creation requires Store.apply_workspace_bundle for one atomic records/objects/
receipt transaction; there is deliberately no misleading multi-transaction fallback.
Template/entry edits use atomic database version/history writes. Entries require CAS;
template clients supply a version for CAS (omission retains legacy compatibility).
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import re
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

MAX_TEMPLATE_BYTES = 128 * 1024
_KEYS = {'schema_version', 'name', 'description', 'phases', 'forms', 'prompts',
         'report_sections', 'work_packages'}
_TOKEN = re.compile(r'{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, limit=200, empty=False):
    if (not isinstance(value, str) or len(value) > limit
            or (not empty and not value.strip())
            or any(ord(c) < 32 and c not in '\n\r\t' for c in value)):
        raise ValueError('工作空间文本无效或超长')
    return value


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError('工作空间配置字段不符合白名单')


def _items(value, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError('工作空间配置列表数量无效')
    return value


def _unique(value, seen):
    if value in seen:
        raise ValueError('工作空间配置key或名称重复')
    seen.add(value)


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('工作空间配置key无效')
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('工作空间日期须为YYYY-MM-DD')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError('工作空间日期须为有效日期') from None
    return value


def validate_template(data):
    """Return an independent validated JSON config, never an executable plugin.

    All object keys are allowlisted; prompts accept only {{content}} and {{title}}.
    Ordinary URLs in descriptive text are inert, not integrations or callbacks.
    """
    _shape(data, _KEYS)
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode('utf-8')) > MAX_TEMPLATE_BYTES:
            raise ValueError('工作空间模板超过128KiB')
    except (TypeError, RecursionError, UnicodeError):
        raise ValueError('工作空间模板必须为有效JSON') from None
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('工作空间模板schema_version必须为1')
    _text(data['name'])
    _text(data['description'], 4000)
    phase_keys = set()
    for phase in _items(data['phases'], 30, 1):
        _shape(phase, {'key', 'name', 'deliverables', 'checklist'})
        _unique(_key(phase['key']), phase_keys)
        _text(phase['name'])
        for value in _items(phase['deliverables'], 50):
            _text(value, 2000)
        for value in _items(phase['checklist'], 50):
            _text(value, 2000)
    forms = set()
    for form in _items(data['forms'], 20):
        _shape(form, {'key', 'name', 'fields'})
        _unique(_key(form['key']), forms)
        _text(form['name'])
        fields = set()
        for field in _items(form['fields'], 50, 1):
            _shape(field, {'key', 'label', 'type', 'required'}, {'options'})
            _unique(_key(field['key']), fields)
            _text(field['label'])
            if field['type'] not in ('text', 'long', 'date', 'choice') or type(field['required']) is not bool:
                raise ValueError('工作空间表单字段类型无效')
            if field['type'] == 'choice':
                seen = set()
                for option in _items(field.get('options'), 50, 1):
                    _unique(_text(option), seen)
            elif 'options' in field:
                raise ValueError('只有choice字段允许options')
    prompts = set()
    for prompt in _items(data['prompts'], 20):
        _shape(prompt, {'name', 'template'})
        _unique(_text(prompt['name']), prompts)
        text = _text(prompt['template'], 12000)
        tokens = _TOKEN.findall(text)
        rest = _TOKEN.sub('', text)
        if ('content' not in tokens or set(tokens) - {'content', 'title'}
                or any(marker in rest for marker in ('{{', '}}', '{%', '%}', '{#', '#}'))):
            raise ValueError('工作空间提示词必须包含content且仅支持content/title变量')
    sections = set()
    for section in _items(data['report_sections'], 30, 1):
        _unique(_text(section), sections)
    packages = set()
    for package in _items(data['work_packages'], 100):
        _shape(package, {'name', 'phase', 'acceptance'})
        _unique(_text(package['name']), packages)
        if not isinstance(package['phase'], str) or package['phase'] not in phase_keys:
            raise ValueError('工作包引用的phase不存在')
        _text(package['acceptance'], 4000)
    return json.loads(encoded)


def builtin_templates():
    """Fresh copies of three modest starter configurations, not compliance advice."""
    specs = [
        ('IPO IT配合', '仅用于资料与整改协作；审计监管要求由专业机构确认，非合规保证。',
         [('inventory', '范围与资料盘点', '系统、权限、制度及证据清单'),
          ('cooperate', '专业机构配合', '经专业机构确认的资料及问题反馈'),
          ('followup', '整改与复核', '整改证据与专业机构复核意见')]),
        ('企业宣传支持', '协调宣传需求、素材授权、技术支持与发布复核，不自动发布。',
         [('brief', '需求确认', '受众、渠道、目标与负责人确认'),
          ('prepare', '素材与技术准备', '素材授权、事实核验和技术验收记录'),
          ('review', '发布复核与总结', '审批意见、发布确认与效果复盘')]),
        ('阶段项目', '按阶段管理范围、交付物、验收与复盘。',
         [('plan', '范围与计划', '范围、负责人、排期及验收口径'),
          ('delivery', '实施与交付', '阶段交付物、测试和问题清单'),
          ('accept', '验收与复盘', '业务验收证据和改进事项')]),
    ]
    result = []
    for name, description, phases in specs:
        result.append(validate_template({
            'schema_version': 1, 'name': name, 'description': description,
            'phases': [{'key': key, 'name': title, 'deliverables': [deliverable],
                        'checklist': ['确认负责人及截止日期', '核验交付物与证据', '记录待确认问题']}
                       for key, title, deliverable in phases],
            'forms': [{'key': 'evidence', 'name': '资料与证据登记', 'fields': [
                {'key': 'subject', 'label': '事项', 'type': 'text', 'required': True},
                {'key': 'evidence', 'label': '证据与说明', 'type': 'long', 'required': True},
                {'key': 'date', 'label': '记录日期', 'type': 'date', 'required': True},
                {'key': 'state', 'label': '核验状态', 'type': 'choice', 'required': True,
                 'options': ['待确认', '已核验', '有缺口']}]}],
            'prompts': [{'name': '专项进展草稿', 'template':
                '仅生成供人工审核的草稿，不执行操作，不编造事实，缺口标注待确认。' + description
                + '\n标题：{{title}}\n根据本专项资料总结阶段进展、证据缺口、风险和下一步：\n{{content}}'}],
            'report_sections': ['阶段进展', '交付物与证据', '风险与问题', '下一步'],
            'work_packages': [{'name': title, 'phase': key, 'acceptance': deliverable}
                              for key, title, deliverable in phases],
        }))
    return result


def _md(value):
    """Render user content as text, not HTML or active Markdown links."""
    text = str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', text).replace('\n', ' / ').replace('\r', '')


class WorkspaceService:
    def __init__(self, store):
        self.store = store
        self._lock = getattr(store, 'lock', RLock())

    def list_templates(self):
        return deepcopy(self.store.objects('workspace_templates'))

    def get_template(self, oid):
        value = self.store.get('workspace_templates', oid)
        if not value:
            raise ValueError('工作空间模板不存在')
        return deepcopy(value)

    def seed_builtin(self):
        with self._lock:
            for index, config in enumerate(builtin_templates()):
                oid = 'workspace-builtin-' + str(index + 1)
                if not self.store.get('workspace_templates', oid):
                    self._save_template(config, oid, None)
        return self.list_templates()

    def _save_template(self, config, oid, old):
        stamp = _now()
        history = deepcopy(old.get('oldhistory', [])) if old else []
        if old:
            history.append({k: deepcopy(v) for k, v in old.items() if k != 'oldhistory'})
        value = {'config': config, 'version': old['version'] + 1 if old else 1,
                 'created': old['created'] if old else stamp, 'updated': stamp,
                 'oldhistory': history}
        return self.store.put('workspace_templates', value, oid)

    def save_template(self, data, oid=None, version=None):
        config = validate_template(data)
        return self.store.save_versioned_object('workspace_templates', {'config': config}, oid, version)

    def export_template(self, oid):
        # Export only the validated config, never histories, records or entries.
        return validate_template(self.get_template(oid)['config'])

    def list_workspaces(self):
        return deepcopy(self.store.objects('workspaces'))

    def get_workspace(self, oid):
        value = self.store.get('workspaces', oid)
        if not value:
            raise ValueError('工作空间不存在')
        return deepcopy(value)

    def create_workspace(self, template_id, name, owner, start_date, due_date, request_id):
        _text(template_id)
        _text(name)
        _text(owner)
        _date(start_date)
        _date(due_date)
        if start_date > due_date:
            raise ValueError('开始日期不能晚于截止日期')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', request_id):
            raise ValueError('工作空间创建需要16—100位幂等请求ID')
        args = dict(template_id=template_id, name=name, owner=owner,
                    start_date=start_date, due_date=due_date)
        wid = 'WORKSPACE-' + uuid5(NAMESPACE_URL, 'itmanager:workspace:' + request_id).hex
        with self._lock:
            old = self.store.get('workspaces', wid)
            if old:
                if old.get('request_args') != args or old.get('request_id') != request_id:
                    raise ValueError('幂等请求ID已用于不同操作')
                return deepcopy(old)
            template = self.get_template(template_id)
            config = validate_template(template['config'])
            project_id = 'PROJECTS-' + uuid5(NAMESPACE_URL, wid + ':project').hex
            records = [{'id': project_id, 'module': 'projects', 'data': {
                '名称': name, '项目负责人': owner, '开始日期': start_date, '截止日期': due_date,
                '状态': '未开始', '阶段': config['phases'][0]['name'],
                '验收标准': '\n'.join(p['acceptance'] for p in config['work_packages'])}}]
            task_ids, task_phases = [], {}
            for index, package in enumerate(config['work_packages']):
                tid = 'TASKS-' + uuid5(NAMESPACE_URL, wid + ':task:' + str(index)).hex
                task_ids.append(tid)
                task_phases[tid] = package['phase']
                records.append({'id': tid, 'module': 'tasks', 'data': {
                    '名称': package['name'], '项目ID': project_id, '负责人': owner,
                    '开始日期': start_date, '截止日期': due_date, '状态': '未开始',
                    '验收标准': package['acceptance']}})
            workspace = dict(id=wid, name=name, owner=owner, start_date=start_date,
                due_date=due_date, template_id=template_id, template_version=template['version'],
                template_snapshot=config, project_id=project_id, task_ids=task_ids,
                task_phases=task_phases, created=_now(), status='active',
                request_id=request_id, request_args=args)
            result = self.store.apply_workspace_bundle(workspace, records, request_id)
            return deepcopy(result['workspace'])

    def list_entries(self, workspace_id):
        self.get_workspace(workspace_id)
        return deepcopy([x for x in self.store.objects('workspace_entries')
                         if x.get('workspace_id') == workspace_id])

    def get_entry(self, workspace_id, oid):
        self.get_workspace(workspace_id)
        entry = self.store.get('workspace_entries', oid)
        if not entry or entry.get('workspace_id') != workspace_id:
            raise ValueError('专项表单记录不存在')
        return deepcopy(entry)

    def save_entry(self, workspace_id, form_key, data, oid=None, version=None, status='draft'):
        with self._lock:
            workspace = self.get_workspace(workspace_id)
            form = next((f for f in workspace['template_snapshot']['forms'] if f['key'] == form_key), None)
            if not form:
                raise ValueError('专项表单不存在')
            fields = {field['key']: field for field in form['fields']}
            if not isinstance(data, dict) or set(data) - set(fields):
                raise ValueError('专项表单存在未知字段')
            if status not in ('draft', 'submitted'):
                raise ValueError('专项表单状态无效')
            clean = {}
            for key, field in fields.items():
                value = data.get(key, '')
                _text(value, 20000 if field['type'] == 'long' else 1000,
                      empty=not field['required'])
                if value:
                    if field['type'] == 'date':
                        _date(value)
                    elif field['type'] == 'choice' and value not in field['options']:
                        raise ValueError('专项表单选项无效')
                clean[key] = value
            if len(json.dumps(clean, ensure_ascii=False).encode('utf-8')) > MAX_TEMPLATE_BYTES:
                raise ValueError('专项表单数据超过128KiB')
            old = self.get_entry(workspace_id, oid) if oid is not None else None
            if old and (old['form_key'] != form_key or type(version) is not int or old['version'] != version):
                raise ValueError('专项表单已更新或类型不符，请刷新')
            if not old and version is not None:
                raise ValueError('新增专项表单不能指定version')
            return self.store.save_versioned_object('workspace_entries', {
                'workspace_id': workspace_id, 'form_key': form_key, 'data': clean, 'status': status}, oid, version)

    def link_record(self, workspace_id, record_id):
        self.get_workspace(workspace_id)
        if not isinstance(record_id, str) or not any(r['id'] == record_id for r in self.store.list_records()):
            raise ValueError('关联记录不存在或已归档')
        oid = uuid5(NAMESPACE_URL, workspace_id + ':link:' + record_id).hex
        return self.store.put('workspace_links', {'workspace_id': workspace_id,
            'record_id': record_id}, oid)

    def related_records(self, workspace_id):
        workspace = self.get_workspace(workspace_id)
        records = self.store.list_records()
        project_id = workspace['project_id']
        # Membership is structural; never infer by title, owner or free text.
        task_ids = {r['id'] for r in records if r['module'] == 'tasks'
                    and r['data'].get('项目ID') == project_id}
        anchors = {project_id} | task_ids
        explicit = {x['record_id'] for x in self.store.objects('workspace_links')
                    if x.get('workspace_id') == workspace_id}
        result = []
        for row in records:
            associated = row['module'] in ('risks', 'logs') and any(
                row['data'].get(key) in anchors for key in ('关联ID', '项目ID', '整改任务ID', '后续任务ID'))
            if row['id'] in anchors or row['id'] in explicit or associated:
                result.append(deepcopy(row))
        return result

    def phase_progress(self, workspace_id):
        workspace = self.get_workspace(workspace_id)
        tasks = {r['id']: r for r in self.related_records(workspace_id) if r['module'] == 'tasks'}
        result = []
        for phase in workspace['template_snapshot']['phases']:
            ids = [tid for tid, key in workspace['task_phases'].items() if key == phase['key']]
            done = sum(tid in tasks and tasks[tid]['data'].get('状态') == '已完成' for tid in ids)
            result.append({'key': phase['key'], 'name': phase['name'], 'total': len(ids),
                           'completed': done, 'missing': sum(tid not in tasks for tid in ids),
                           'percent': round(done * 100 / len(ids), 1) if ids else 0})
        return result

    def report(self, workspace_id):
        workspace = self.get_workspace(workspace_id)
        config = workspace['template_snapshot']
        lines = ['# ' + _md(workspace['name']), '', _md(config['description']), '',
                 '负责人：' + _md(workspace['owner']),
                 '计划：' + workspace['start_date'] + ' — ' + workspace['due_date'],
                 '模板版本：' + str(workspace['template_version']), '', '## 阶段进展']
        for phase, progress in zip(config['phases'], self.phase_progress(workspace_id)):
            lines.extend(['### ' + _md(phase['name']),
                '- 已完成：{completed}/{total}（{percent}%）；缺失或归档：{missing}'.format(**progress),
                '- 交付物：' + '；'.join(_md(v) for v in phase['deliverables'])])
            lines.extend('- 待人工核验：' + _md(v) for v in phase['checklist'])
        lines.extend(['', '## 专项关联记录'])
        for record in self.related_records(workspace_id):
            lines.append('### ' + _md(record['module'] + ' / ' + record['data'].get('名称', record['id'])))
            lines.append('- ID：' + _md(record['id']))
            lines.extend('- ' + _md(k) + '：' + _md(v) for k, v in record['data'].items() if v)
        lines.extend(['', '## 专项表单'])
        forms = {f['key']: f for f in config['forms']}
        for entry in self.list_entries(workspace_id):
            form = forms[entry['form_key']]
            labels = {f['key']: f['label'] for f in form['fields']}
            lines.append('### ' + _md(form['name']) + '（' + _md(entry['status']) + '）')
            lines.extend('- ' + _md(labels[k]) + '：' + _md(v) for k, v in entry['data'].items())
        lines.extend(['', '## 汇报复核提纲'])
        lines.extend('- ' + _md(section) + '：请根据上述证据人工核验，缺失信息标注待确认。'
                     for section in config['report_sections'])
        return '\n'.join(lines) + '\n'

    render_report = report

    def render_context(self, workspace_id, prompt_name=None):
        workspace = self.get_workspace(workspace_id)
        prompts = workspace['template_snapshot']['prompts']
        prompt = next((p for p in prompts if p['name'] == prompt_name), None) if prompt_name is not None else (prompts[0] if prompts else None)
        if prompt is None:
            raise ValueError('专项提示词不存在')
        return {'workspace_id': workspace_id, 'title': workspace['name'],
                'context': self.report(workspace_id), 'template': prompt['template']}
