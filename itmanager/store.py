"""SQLite repository; business writes, history and audit share one transaction."""
import json
import hashlib
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from .schema import MODULES

SCHEMA_VERSION = 1
REFERENCES = {'规划ID': 'plans', '项目ID': 'projects', '父任务ID': 'tasks',
              '人员ID': 'people', '系统ID': 'systems', '整改任务ID': 'tasks',
              '后续任务ID': 'tasks', '关联ID': None, '来源ID': None}
MONEY = {'预算', '金额', '预计费用', '已发生', '已承诺未发生', '预计新增'}
NUMBERS = MONEY | {'评分', 'RTO小时', 'RPO小时', '实际耗时', '实际数据缺口', '工作量'}


def now():
    return datetime.now(timezone.utc).isoformat()


def unpack(row):
    return dict(row, data=json.loads(row['data']))


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path == ':memory:':
            raise ValueError('请使用本地SQLite文件（测试可使用临时目录）')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = db.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0] if 'schema_versions' in tables else 0
            version = version or 0
            if version > SCHEMA_VERSION:
                raise ValueError('数据库版本高于当前程序，拒绝启动')
            if version < SCHEMA_VERSION:
                if 'records' in tables:
                    target = Path(self.path).with_name(Path(self.path).name + '.pre-v1-' + uuid4().hex[:8] + '.bak')
                    with sqlite3.connect(target) as dst:
                        db.backup(dst)
                db.execute('BEGIN IMMEDIATE')
                statements = [
                    'CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,module TEXT NOT NULL,data TEXT NOT NULL,version INTEGER NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL)',
                    'CREATE INDEX IF NOT EXISTS record_module ON records(module)',
                    'CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,time TEXT,action TEXT,object_id TEXT,detail TEXT)',
                    'CREATE TABLE IF NOT EXISTS objects(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id))',
                    'CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,schedule_id TEXT,slot TEXT,state TEXT,data TEXT,UNIQUE(schedule_id,slot))',
                    'CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY,applied TEXT NOT NULL)',
                    'CREATE TABLE IF NOT EXISTS record_history(id TEXT NOT NULL,version INTEGER NOT NULL,module TEXT NOT NULL,data TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(id,version))',
                ]
                for sql in statements:
                    db.execute(sql)
                if 'archived' not in {r[1] for r in db.execute('PRAGMA table_info(records)')}:
                    db.execute('ALTER TABLE records ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
                db.execute('INSERT OR IGNORE INTO record_history SELECT id,version,module,data,created,updated,archived FROM records')
                db.execute('INSERT INTO schema_versions VALUES(?,?)', (SCHEMA_VERSION, now()))
                db.commit()
            db.execute('PRAGMA journal_mode=WAL')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, db, action, oid, detail=''):
        db.execute('INSERT INTO audit(time,action,object_id,detail) VALUES(?,?,?,?)', (now(), action, oid, detail))

    def record_audit(self, action, oid, detail=''):
        """Append a standalone audit event without exposing a DB connection."""
        with self.lock, self.connect() as db:
            self.audit(db, action, oid, detail)

    def list_records(self, module=None, include_archived=False):
        with self.connect() as db:
            sql = 'SELECT * FROM records WHERE 1=1'
            args = []
            if not include_archived:
                sql += ' AND archived=0'
            if module:
                sql += ' AND module=?'
                args.append(module)
            return [unpack(r) for r in db.execute(sql + ' ORDER BY updated DESC,id', args)]

    def _validate(self, module, data, oid, records):
        if not isinstance(module, str) or module not in MODULES:
            raise ValueError('未知业务模块')
        if not isinstance(data, dict) or any(k not in MODULES[module][1] for k in data):
            raise ValueError('字段不符合模块定义')
        if any(not isinstance(v, str) or len(v) > 50000 for v in data.values()):
            raise ValueError('字段必须为文本且不超过50000字符')
        if not data.get(MODULES[module][1][0], '').strip():
            raise ValueError('名称不能为空')
        for key, value in data.items():
            if not value:
                continue
            if key.endswith('日期'):
                try:
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                        raise ValueError()
                    datetime.strptime(value, '%Y-%m-%d')
                except ValueError:
                    raise ValueError(key + '应为有效的YYYY-MM-DD') from None
            if key in {'受理时间', '恢复时间'}:
                try:
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?', value):
                        raise ValueError()
                    datetime.fromisoformat(value)
                except ValueError:
                    raise ValueError(key + '应为有效ISO日期时间') from None
            if key in NUMBERS:
                try:
                    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', value):
                        raise InvalidOperation()
                    number = Decimal(value)
                    if not number.is_finite() or number < 0:
                        raise InvalidOperation()
                except InvalidOperation:
                    raise ValueError(key + '应为非负十进制数值') from None
            if key in REFERENCES:
                ref = records.get(value)
                if not ref or ref.get('archived') or (REFERENCES[key] and ref['module'] != REFERENCES[key]):
                    raise ValueError(key + '引用记录不存在或模块不符')
        if data.get('开始日期') and data.get('截止日期') and data['开始日期'] > data['截止日期']:
            raise ValueError('开始日期不能晚于截止日期')
        if module == 'reviews':
            if data.get('评分') and Decimal(data['评分']) > 100:
                raise ValueError('评分应在0—100之间')
            if data.get('状态') and data['状态'] not in {'目标设定', '待评价', '待反馈', '待复核', '已定稿'}:
                raise ValueError('考核状态无效')
            if data.get('状态') == '已定稿':
                required = ('人员ID', '考核周期', '成果证据', '主管评价', '员工反馈', '复核人')
                missing = [key for key in required if not data.get(key, '').strip()]
                if missing:
                    raise ValueError('考核定稿缺少: ' + '、'.join(missing))
        if module == 'tasks':
            if data.get('状态') == '已完成' and (not data.get('完成证据', '').strip() or not data.get('验收人', '').strip()):
                raise ValueError('完成任务需要完成证据和验收人')
            seen = {oid}
            parent = data.get('父任务ID')
            while parent:
                if parent in seen:
                    raise ValueError('父任务不能形成环')
                seen.add(parent)
                row = records.get(parent)
                if not row or row['module'] != 'tasks' or row.get('archived'):
                    raise ValueError('父任务ID引用记录不存在')
                pdata = row['data']
                if pdata.get('项目ID', '') != data.get('项目ID', ''):
                    raise ValueError('父子任务必须关联同一项目')
                parent = pdata.get('父任务ID')
            # Changing the parent project must not invalidate existing children.
            for row in records.values():
                if row['module'] == 'tasks' and not row.get('archived') and row['data'].get('父任务ID') == oid:
                    if row['data'].get('项目ID', '') != data.get('项目ID', ''):
                        raise ValueError('父子任务必须关联同一项目')

    def _snapshot(self, db, oid):
        db.execute('INSERT INTO record_history SELECT id,version,module,data,created,updated,archived FROM records WHERE id=?', (oid,))

    def save_record(self, module, data, oid=None, version=None):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            records = {r['id']: unpack(r) for r in db.execute('SELECT * FROM records')}
            old = records.get(oid) if oid else None
            if oid:
                if not old or old['module'] != module or old['archived']:
                    raise ValueError('记录不存在')
                if type(version) is not int or old['version'] != version:
                    raise ValueError('记录已被更新，请刷新后重试')
            else:
                oid = str(module).upper() + '-' + uuid4().hex[:12]
            candidate = {'id': oid, 'module': module, 'data': data, 'archived': 0}
            records[oid] = candidate
            self._validate(module, data, oid, records)
            if old and module == 'reviews' and old['data'].get('状态') == '已定稿':
                # Reopen explicitly before changing the approved evaluation.
                reopened = dict(old['data'], 状态='待复核')
                if data != reopened:
                    raise ValueError('已定稿考核须先仅将状态转为待复核，再修改内容')
            stamp = now()
            if old:
                db.execute('UPDATE records SET data=?,version=version+1,updated=? WHERE id=?', (json.dumps(data, ensure_ascii=False), stamp, oid))
            else:
                db.execute('INSERT INTO records(id,module,data,version,created,updated) VALUES(?,?,?,?,?,?)', (oid, module, json.dumps(data, ensure_ascii=False), 1, stamp, stamp))
            self._snapshot(db, oid)
            self.audit(db, '保存记录', oid)
            result = unpack(db.execute('SELECT * FROM records WHERE id=?', (oid,)).fetchone())
        return result

    @staticmethod
    def _operation_text(data, allowed, required=()):
        if not isinstance(data, dict) or any(k not in allowed for k in data):
            raise ValueError('操作字段不符合白名单')
        if any(not isinstance(v, str) or len(v) > 50000 for v in data.values()):
            raise ValueError('操作字段必须为文本且不超过50000字符')
        if any(not data.get(k, '').strip() for k in required):
            raise ValueError('必填字段不能为空: ' + ','.join(required))

    @staticmethod
    def _operation_request(request_id, operation, arguments):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', request_id):
            raise ValueError('操作必须提供16—100位字母数字_-幂等请求ID')
        try:
            encoded = json.dumps([operation, arguments], ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError('操作输入格式无效') from None
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def _operation_replay(self, db, request_id, fingerprint):
        row = db.execute("SELECT data FROM objects WHERE kind='operation_receipts' AND id=?", (request_id,)).fetchone()
        if row:
            receipt = json.loads(row['data'])
            if receipt['fingerprint'] != fingerprint:
                raise ValueError('幂等请求ID已用于不同操作')
            return receipt['result']
        return None

    def _operation_receipt(self, db, request_id, fingerprint, result):
        receipt = {'fingerprint': fingerprint, 'result': result, 'created': now()}
        db.execute('INSERT INTO objects VALUES(?,?,?)', ('operation_receipts', request_id, json.dumps(receipt, ensure_ascii=False)))
        return result

    @staticmethod
    def _operation_row(module, data):
        stamp = now()
        return {'id': module.upper() + '-' + uuid4().hex[:12], 'module': module,
                'data': data, 'version': 1, 'created': stamp, 'updated': stamp, 'archived': 0}

    def _operation_write(self, db, rows, records, updated_id=None):
        # Validate the entire batch (including forward references) before any write.
        combined = dict(records)
        combined.update({row['id']: row for row in rows})
        for row in rows:
            self._validate(row['module'], row['data'], row['id'], combined)
        for row in rows:
            if row['id'] == updated_id:
                cursor = db.execute('UPDATE records SET data=?,version=?,updated=? WHERE id=? AND version=? AND archived=0',
                                    (json.dumps(row['data'], ensure_ascii=False), row['version'], row['updated'], row['id'], row['version'] - 1))
                if cursor.rowcount != 1:
                    raise ValueError('记录已被更新，请刷新后重试')
            else:
                db.execute('INSERT INTO records(id,module,data,version,created,updated,archived) VALUES(?,?,?,?,?,?,?)',
                           (row['id'], row['module'], json.dumps(row['data'], ensure_ascii=False), row['version'], row['created'], row['updated'], row['archived']))
            self._snapshot(db, row['id'])
            self.audit(db, '管理操作', row['id'])

    def _operation_log(self, task, title, content, result, evidence=''):
        return self._operation_row('logs', {'名称': title, '日期': now()[:10], '关联ID': task['id'],
            '工作内容': content, '结果': result, '下一步': task['data'].get('下一步', ''), '证据': evidence})

    def task_action(self, oid, version, action, payload, request_id):
        fingerprint = self._operation_request(request_id, 'task_action', [oid, version, action, payload])
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._operation_replay(db, request_id, fingerprint)
            if replay is not None:
                return replay
            allowed = {
                'progress': {'next_step', 'status', 'content', 'result', 'evidence'},
                'block': {'reason', 'next_step', 'content', 'result', 'evidence'},
                'submit': {'evidence', 'acceptor', 'next_step', 'content', 'result'},
                'complete': {'evidence', 'acceptor', 'next_step', 'content', 'result'},
                'postpone': {'due_date', 'reason', 'approver', 'next_step', 'content', 'result', 'evidence'},
            }
            if not isinstance(action, str) or action not in allowed:
                raise ValueError('未知任务动作')
            required = {'progress': ('next_step',), 'block': ('reason', 'next_step'),
                        'submit': ('evidence', 'acceptor'), 'complete': (), 'postpone': ('due_date', 'reason', 'approver')}
            self._operation_text(payload, allowed[action], required[action])
            if not isinstance(oid, str) or not oid.strip() or len(oid) > 200:
                raise ValueError('任务ID无效')
            records = {r['id']: unpack(r) for r in db.execute('SELECT * FROM records')}
            old = records.get(oid)
            if not old or old['module'] != 'tasks' or old['archived']:
                raise ValueError('任务不存在')
            if type(version) is not int or old['version'] != version:
                raise ValueError('记录已被更新，请刷新后重试')
            if old['data'].get('状态') in {'已完成', '已取消'}:
                raise ValueError('已完成或已取消任务不可执行管理动作')
            data = dict(old['data'])
            for key, field in {'next_step': '下一步', 'evidence': '完成证据', 'acceptor': '验收人'}.items():
                if key in payload:
                    data[field] = payload[key]
            if action == 'progress':
                if 'status' in payload:
                    if payload['status'] != '进行中':
                        raise ValueError('进展状态只能为进行中')
                    data['状态'] = '进行中'
            elif action == 'block':
                data.update(状态='阻塞', 阻塞原因=payload['reason'])
            elif action in {'submit', 'complete'}:
                data['状态'] = '待验收' if action == 'submit' else '已完成'
            else:
                data['截止日期'] = payload['due_date']
            task = dict(old, data=data, version=version + 1, updated=now())
            # Structured original/new date, reason and approver remain in the immutable log/history.
            detail = dict(payload)
            if action == 'postpone':
                detail.update(original_due_date=old['data'].get('截止日期', ''), new_due_date=data['截止日期'])
            log = self._operation_log(task, '任务操作:' + action + ' ' + data['名称'],
                json.dumps(detail, ensure_ascii=False), payload.get('result') or data.get('状态', '') or action,
                payload.get('evidence', data.get('完成证据', '')))
            self._operation_write(db, [task, log], records, oid)
            return self._operation_receipt(db, request_id, fingerprint, {'task': task, 'log': log})

    def create_actions(self, source_id, items, request_id):
        fingerprint = self._operation_request(request_id, 'create_actions', [source_id, items])
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._operation_replay(db, request_id, fingerprint)
            if replay is not None:
                return replay
            if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
                raise ValueError('来源ID无效')
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise ValueError('行动项须为1—20项列表')
            for item in items:
                self._operation_text(item, {'name', 'owner', 'due_date', 'next_step', 'acceptance'}, ('name',))
            records = {r['id']: unpack(r) for r in db.execute('SELECT * FROM records')}
            source = records.get(source_id)
            if source_id.startswith('ai:'):
                if not db.execute("SELECT id FROM objects WHERE kind='ai_calls' AND id=?", (source_id[3:],)).fetchone():
                    raise ValueError('AI调用来源不存在')
                source = None
            elif not source or source['archived']:
                raise ValueError('来源记录不存在')
            links = {}
            if source:
                sd = source['data']
                project = source_id if source['module'] == 'projects' else sd.get('项目ID', '')
                parent = source_id if source['module'] == 'tasks' else sd.get('父任务ID', '')
                if parent:
                    links['父任务ID'] = parent
                    if not project and records.get(parent):
                        project = records[parent]['data'].get('项目ID', '')
                if project:
                    links['项目ID'] = project
            tasks, logs = [], []
            for item in items:
                data = {'名称': item['name'], '状态': '未开始', '管理建议': '来源: ' + source_id, **links}
                for key, field in {'owner': '负责人', 'due_date': '截止日期', 'next_step': '下一步', 'acceptance': '验收标准'}.items():
                    if key in item:
                        data[field] = item[key]
                task = self._operation_row('tasks', data)
                tasks.append(task)
                logs.append(self._operation_log(task, '创建行动项: ' + item['name'], '来源: ' + source_id, '已创建', source_id))
            self._operation_write(db, tasks + logs, records)
            return self._operation_receipt(db, request_id, fingerprint, {'tasks': tasks, 'logs': logs})

    def complete_checklist(self, data, request_id):
        fingerprint = self._operation_request(request_id, 'complete_checklist', data)
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._operation_replay(db, request_id, fingerprint)
            if replay is not None:
                return replay
            if not isinstance(data, dict) or any(k not in {'name', 'owner', 'date', 'items'} for k in data):
                raise ValueError('检查表字段不符合白名单')
            self._operation_text({k: v for k, v in data.items() if k != 'items'}, {'name', 'owner', 'date'}, ('name', 'date'))
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', data['date']):
                raise ValueError('检查日期应为有效YYYY-MM-DD')
            try:
                datetime.strptime(data['date'], '%Y-%m-%d')
            except ValueError:
                raise ValueError('检查日期应为有效YYYY-MM-DD') from None
            items = data.get('items')
            if not isinstance(items, list) or not items:
                raise ValueError('检查项须为非空列表')
            for item in items:
                self._operation_text(item, {'name', 'result', 'note'}, ('name', 'result'))
                if item['result'] not in {'通过', '异常', '不适用'}:
                    raise ValueError('检查结果无效')
                if item['result'] == '异常' and not item.get('note', '').strip():
                    raise ValueError('异常检查项必须填写note')
            records = {r['id']: unpack(r) for r in db.execute('SELECT * FROM records')}
            run_id = 'CHECKLIST-' + uuid4().hex[:12]
            tasks, risks, logs = [], [], []
            for item in items:
                if item['result'] != '异常':
                    continue
                risk = self._operation_row('risks', {'名称': item['name'], '风险类别': '检查异常',
                    '影响': item['note'], '负责人': data.get('owner', ''), '状态': '待整改'})
                task = self._operation_row('tasks', {'名称': '整改: ' + item['name'], '负责人': data.get('owner', ''),
                    '状态': '未开始', '下一步': item['note'], '验收标准': '复核检查项通过并提供整改证据',
                    '管理建议': '来源: ' + run_id + '；风险ID: ' + risk['id']})
                risk['data'].update(关联ID=task['id'], 长期措施='来源: ' + run_id + '；整改任务ID: ' + task['id'])
                tasks.append(task)
                risks.append(risk)
                logs.append(self._operation_log(task, '检查异常整改: ' + item['name'],
                    '来源: ' + run_id + '；风险ID: ' + risk['id'] + '；' + item['note'], '已创建风险与整改任务', item['note']))
            self._operation_write(db, tasks + risks + logs, records)
            run = dict(data, id=run_id, created=now(), task_ids=[t['id'] for t in tasks], risk_ids=[r['id'] for r in risks])
            db.execute('INSERT INTO objects VALUES(?,?,?)', ('checklist_runs', run_id, json.dumps(run, ensure_ascii=False)))
            self.audit(db, '完成检查表', run_id)
            return self._operation_receipt(db, request_id, fingerprint, {'run': run, 'tasks': tasks, 'risks': risks})

    def apply_workspace_bundle(self, workspace_data, records, request_id):
        """Create a workspace snapshot and all its records in one transaction."""
        # Template services regenerate timestamps/rows on retries; the original
        # user arguments, not regenerated artifacts, define that request.
        arguments = workspace_data.get('request_args') if isinstance(workspace_data, dict) else None
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError('工作体系request_args必须为对象')
        fingerprint = self._operation_request(request_id, 'apply_workspace_bundle',
                                              arguments if arguments is not None else [workspace_data, records])
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._operation_replay(db, request_id, fingerprint)
            if replay is not None:
                return replay
            if not isinstance(workspace_data, dict):
                raise ValueError('工作体系配置必须为对象')
            workspace = dict(workspace_data)
            oid = workspace.get('id', 'WORKSPACE-' + uuid4().hex[:12])
            if not isinstance(oid, str) or not oid.strip() or len(oid) > 200:
                raise ValueError('工作体系ID无效')
            if db.execute("SELECT id FROM objects WHERE kind='workspaces' AND id=?", (oid,)).fetchone():
                raise ValueError('工作体系已存在')
            if not isinstance(records, list) or not records:
                raise ValueError('工作体系records须为非空列表')
            prepared, errors = self._prepare_import(db, records)
            if errors:
                raise ValueError('工作体系校验失败: ' + json.dumps(errors, ensure_ascii=False))
            workspace.update(id=oid)
            for row in prepared:
                db.execute('INSERT INTO records(id,module,data,version,created,updated,archived) VALUES(?,?,?,?,?,?,?)',
                           (row['id'], row['module'], json.dumps(row['data'], ensure_ascii=False), row['version'], row['created'], row['updated'], row['archived']))
                self._snapshot(db, row['id'])
                self.audit(db, '创建工作体系记录', row['id'], oid)
            db.execute('INSERT INTO objects VALUES(?,?,?)', ('workspaces', oid, json.dumps(workspace, ensure_ascii=False)))
            self.audit(db, '创建工作体系', oid)
            return self._operation_receipt(db, request_id, fingerprint, {'workspace': workspace, 'records': prepared})

    def record_history(self, oid):
        with self.connect() as db:
            return [unpack(r) for r in db.execute('SELECT * FROM record_history WHERE id=? ORDER BY version', (oid,))]

    history = record_history

    def archive_record(self, oid, version):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM records WHERE id=? AND archived=0', (oid,)).fetchone()
            if not row:
                raise ValueError('记录不存在')
            if type(version) is not int or row['version'] != version:
                raise ValueError('记录已被更新，请刷新后重试')
            for other in db.execute('SELECT * FROM records WHERE archived=0 AND id<>?', (oid,)):
                data = json.loads(other['data'])
                if any(data.get(key) == oid for key in REFERENCES):
                    raise ValueError('记录仍被有效记录引用，不能归档')
            db.execute('UPDATE records SET archived=1,version=version+1,updated=? WHERE id=?', (now(), oid))
            self._snapshot(db, oid)
            self.audit(db, '归档记录', oid)
            return unpack(db.execute('SELECT * FROM records WHERE id=?', (oid,)).fetchone())

    def batch_update_records(self, updates, request_id):
        """All-or-nothing batch field update with per-record CAS; idempotent by request_id."""
        if not isinstance(updates, list) or not 1 <= len(updates) <= 200:
            raise ValueError('批量更新须为1—200条')
        normalized = []
        for item in updates:
            if not isinstance(item, dict) or set(item) != {'id', 'version', 'data'}:
                raise ValueError('批量更新项格式无效')
            oid, version, data = item['id'], item['version'], item['data']
            if not isinstance(oid, str) or not oid.strip() or len(oid) > 200:
                raise ValueError('批量更新记录ID无效')
            if type(version) is not int or version < 1:
                raise ValueError('批量更新版本无效')
            if not isinstance(data, dict):
                raise ValueError('批量更新数据无效')
            normalized.append({'id': oid, 'version': version, 'data': data})
        ids = [item['id'] for item in normalized]
        if len(set(ids)) != len(ids):
            raise ValueError('批量更新存在重复记录')
        fingerprint = self._operation_request(request_id, 'batch_update', normalized)
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._operation_replay(db, request_id, fingerprint)
            if replay is not None:
                return replay
            records = {r['id']: unpack(r) for r in db.execute('SELECT * FROM records')}
            stamp = now()
            results = []
            for item in normalized:
                old = records.get(item['id'])
                if not old or old['archived']:
                    raise ValueError('记录不存在')
                if old['version'] != item['version']:
                    raise ValueError('记录已被更新，请刷新后重试')
                if old['module'] == 'reviews' and old['data'].get('状态') == '已定稿':
                    reopened = dict(old['data'], 状态='待复核')
                    if item['data'] != reopened:
                        raise ValueError('已定稿考核须先仅将状态转为待复核，再修改内容')
                candidate = dict(old, data=item['data'], version=old['version'] + 1)
                records[candidate['id']] = candidate
                # Validate the whole batch so cross-record references stay consistent.
                self._validate(old['module'], item['data'], old['id'], records)
                results.append({'id': candidate['id'], 'module': old['module'], 'version': candidate['version']})
            for item in normalized:
                row = records[item['id']]
                cursor = db.execute('UPDATE records SET data=?,version=?,updated=? WHERE id=? AND version=? AND archived=0',
                                    (json.dumps(row['data'], ensure_ascii=False), row['version'], stamp,
                                     row['id'], item['version']))
                if cursor.rowcount != 1:
                    raise ValueError('记录已被更新，请刷新后重试')
                self._snapshot(db, row['id'])
                self.audit(db, '批量更新', row['id'])
            return self._operation_receipt(db, request_id, fingerprint,
                                           {'records': results, 'count': len(results)})

    def batch_archive_records(self, targets, request_id):
        """All-or-nothing batch archive; refuses while a live record still references it."""
        if not isinstance(targets, list) or not 1 <= len(targets) <= 200:
            raise ValueError('批量归档须为1—200条')
        normalized = []
        for item in targets:
            if not isinstance(item, dict) or set(item) != {'id', 'version'}:
                raise ValueError('批量归档项格式无效')
            oid, version = item['id'], item['version']
            if not isinstance(oid, str) or not oid.strip() or len(oid) > 200:
                raise ValueError('批量归档记录ID无效')
            if type(version) is not int or version < 1:
                raise ValueError('批量归档版本无效')
            normalized.append({'id': oid, 'version': version})
        ids = [item['id'] for item in normalized]
        if len(set(ids)) != len(ids):
            raise ValueError('批量归档存在重复记录')
        fingerprint = self._operation_request(request_id, 'batch_archive', normalized)
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            replay = self._operation_replay(db, request_id, fingerprint)
            if replay is not None:
                return replay
            stamp = now()
            done = []
            for item in normalized:
                row = db.execute('SELECT * FROM records WHERE id=? AND archived=0', (item['id'],)).fetchone()
                if not row:
                    raise ValueError('记录不存在')
                if row['version'] != item['version']:
                    raise ValueError('记录已被更新，请刷新后重试')
                for other in db.execute('SELECT * FROM records WHERE archived=0 AND id<>?', (item['id'],)):
                    data = json.loads(other['data'])
                    if any(data.get(key) == item['id'] for key in REFERENCES):
                        raise ValueError('记录仍被有效记录引用，不能归档')
                done.append(item['id'])
            for oid in done:
                db.execute('UPDATE records SET archived=1,version=version+1,updated=? WHERE id=?', (stamp, oid))
                self._snapshot(db, oid)
                self.audit(db, '批量归档', oid)
            return self._operation_receipt(db, request_id, fingerprint, {'archived': done, 'count': len(done)})

    def get_record(self, oid):
        """One record by id, including archived rows (needed for attachment ownership)."""
        if not isinstance(oid, str) or not oid:
            return None
        with self.connect() as db:
            row = db.execute('SELECT * FROM records WHERE id=?', (oid,)).fetchone()
            return unpack(row) if row else None

    def page_records(self, module=None, query='', owner='', status='', start='', end='',
                     limit=100, offset=0, include_archived=False):
        """Server-side filter + slice so large ledgers need not travel whole.

        Filtering happens before slicing; ``total`` is the number of matches, not the
        number of rows returned. Sorting stays stable (updated desc, id asc).
        """
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('分页条数须为1—500')
        if type(offset) is not int or offset < 0:
            raise ValueError('分页偏移无效')
        if module is not None and module not in MODULES:
            raise ValueError('未知业务模块')
        for value, label in ((query, '搜索'), (owner, '责任人'), (status, '状态'), (start, '开始日期'), (end, '结束日期')):
            if value is not None and not isinstance(value, str):
                raise ValueError('分页' + label + '条件无效')
            if value and len(value) > 200:
                raise ValueError('分页' + label + '条件过长')
        rows = self.list_records(module, include_archived=include_archived)
        needle = (query or '').strip().lower()
        owner_needle = (owner or '').strip().lower()
        status_needle = (status or '').strip()
        start, end = (start or '').strip()[:10], (end or '').strip()[:10]
        matches = []
        for row in rows:
            data = row.get('data', {})
            if needle:
                haystack = ' '.join([row.get('id', ''), row.get('module', '')]
                                    + [str(value) for value in data.values()]).lower()
                if needle not in haystack:
                    continue
            if owner_needle and owner_needle not in self._owner_text(row).lower():
                continue
            if status_needle and str(data.get('状态') or data.get('核验状态') or data.get('结论') or '') != status_needle:
                continue
            if start or end:
                day = self._business_date(row)
                if not day or (start and day < start) or (end and day > end):
                    continue
            matches.append(row)
        matches.sort(key=lambda row: (str(row.get('updated') or ''), str(row.get('id') or '')), reverse=True)
        page = matches[offset:offset + limit]
        return {'records': page, 'total': len(matches), 'limit': limit, 'offset': offset,
                'has_more': offset + limit < len(matches)}

    @staticmethod
    def _owner_text(row):
        data = row.get('data', {})
        return ' / '.join(str(data.get(key)) for key in
                          ('负责人', '项目负责人', '业务负责人', '技术负责人', '责任人', '姓名',
                           '执行人', '提供人', '提出人', '决策人', '复核人', '验收人') if data.get(key))

    @staticmethod
    def _business_date(row):
        data = row.get('data', {})
        for field in ('截止日期', '日期', '到期日期', '维保到期日期', '复核日期', '演练日期',
                      '受理时间', '计划窗口', '开始日期', '采购日期'):
            raw = str(data.get(field) or '')[:10]
            try:
                return date.fromisoformat(raw).isoformat()
            except ValueError:
                continue
        return ''

    def _prepare_import(self, db, records):
        if not isinstance(records, list):
            raise ValueError('导入records必须为列表')
        existing = {r['id']: unpack(r) for r in db.execute('SELECT * FROM records')}
        combined = dict(existing)
        prepared, errors = [], []
        for index, raw in enumerate(records):
            try:
                if not isinstance(raw, dict):
                    raise ValueError('记录必须为对象')
                oid = raw.get('id')
                if not isinstance(oid, str) or not oid.strip() or len(oid) > 200:
                    raise ValueError('记录ID无效')
                if oid in combined:
                    raise ValueError('重复ID，默认拒绝覆盖')
                if type(raw.get('version', 1)) is not int or raw.get('version', 1) < 1:
                    raise ValueError('version必须为正整数')
                stamp = now()
                row = dict(id=oid, module=raw.get('module'), data=raw.get('data'), version=raw.get('version', 1), created=raw.get('created', stamp), updated=raw.get('updated', stamp), archived=raw.get('archived', 0))
                if type(row['archived']) is not int or row['archived'] not in (0, 1):
                    raise ValueError('archived必须为0或1')
                for key in ('created', 'updated'):
                    if not isinstance(row[key], str):
                        raise ValueError('时间必须为带时区ISO格式')
                    try:
                        parsed = datetime.fromisoformat(row[key])
                    except ValueError:
                        raise ValueError('时间必须为带时区ISO格式') from None
                    if parsed.tzinfo is None:
                        raise ValueError('时间必须为带时区ISO格式')
                    row[key] = parsed.astimezone(timezone.utc).isoformat()
                if row['created'] > row['updated']:
                    raise ValueError('创建时间不能晚于更新时间')
                # Shape validation before references are traversed.
                if not isinstance(row['module'], str) or row['module'] not in MODULES or not isinstance(row['data'], dict):
                    raise ValueError('模块或data无效')
                if any(k not in MODULES[row['module']][1] for k in row['data']) or any(not isinstance(v, str) or len(v) > 50000 for v in row['data'].values()):
                    raise ValueError('字段不符合模块定义或文本格式')
                combined[oid] = row
                prepared.append((index, row))
            except ValueError as exc:
                errors.append({'index': index, 'id': raw.get('id') if isinstance(raw, dict) else None, 'message': str(exc)})
        for index, row in prepared:
            try:
                self._validate(row['module'], row['data'], row['id'], combined)
            except ValueError as exc:
                errors.append({'index': index, 'id': row['id'], 'message': str(exc)})
        return [row for _, row in prepared], errors

    def import_preview(self, records):
        with self.connect() as db:
            db.execute('BEGIN')
            prepared, errors = self._prepare_import(db, records)
            return {'valid': not errors, 'count': len(records), 'errors': errors}

    def import_records(self, records):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prepared, errors = self._prepare_import(db, records)
            if errors:
                raise ValueError('导入校验失败: ' + json.dumps(errors, ensure_ascii=False))
            batch = uuid4().hex
            for row in prepared:
                db.execute('INSERT INTO records(id,module,data,version,created,updated,archived) VALUES(?,?,?,?,?,?,?)', (row['id'], row['module'], json.dumps(row['data'], ensure_ascii=False), row['version'], row['created'], row['updated'], row['archived']))
                self._snapshot(db, row['id'])
                self.audit(db, '导入记录', row['id'], '批次:' + batch)
            self.audit(db, '导入批次', batch, '记录数:' + str(len(prepared)))
        return {'imported': len(prepared)}

    def objects(self, kind):
        with self.connect() as db:
            return [dict(json.loads(r['data']), id=r['id']) for r in db.execute('SELECT * FROM objects WHERE kind=?', (kind,))]

    def get(self, kind, oid):
        return next((x for x in self.objects(kind) if x['id'] == oid), None)

    def put(self, kind, data, oid=None):
        # Transaction receipts and completed checklists are append-only internal objects.
        if kind in {'operation_receipts', 'checklist_runs'}:
            raise ValueError('管理操作回执和已完成检查表不可修改')
        # Keep generic upsert semantics, but payload ID must never redirect a write.
        data = dict(data)
        payload_id = data.pop('id', None)
        if payload_id is not None and payload_id != oid:
            raise ValueError('对象ID与目标ID不一致')
        oid = oid or uuid4().hex
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data', (kind, oid, json.dumps(data, ensure_ascii=False)))
            self.audit(db, '保存' + kind, oid)
        return dict(data, id=oid)

    def delete_object(self, kind, oid):
        """Remove one configuration object; business records are never deleted here."""
        if kind in {'operation_receipts', 'checklist_runs'}:
            raise ValueError('管理操作回执和已完成检查表不可修改')
        if not isinstance(oid, str) or not oid:
            raise ValueError('对象ID无效')
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM objects WHERE kind=? AND id=?', (kind, oid)).fetchone()
            if not row:
                raise ValueError('对象不存在')
            db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, oid))
            self.audit(db, '删除' + kind, oid)
        return {'deleted': True, 'id': oid}

    def save_versioned_object(self, kind, data, oid=None, expected_version=None, *, prompt_defaults=False):
        """Atomic object CAS/history/default transition; omitted version is legacy mode."""
        if kind not in {'workspace_templates', 'workspace_entries', 'prompts', 'reminders', 'checklist_templates'}:
            raise ValueError('不支持版本化对象类型')
        data = dict(data)
        payload_id = data.pop('id', None)
        if payload_id is not None and payload_id != oid:
            raise ValueError('对象ID与目标ID不一致')
        creating = oid is None
        oid = oid or uuid4().hex
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM objects WHERE kind=? AND id=?', (kind, oid)).fetchone()
            old = json.loads(row['data']) if row else None
            if not creating and old is None:
                raise ValueError('版本化对象不存在')
            if expected_version is not None and (type(expected_version) is not int or not old or old['version'] != expected_version):
                raise ValueError('记录已被更新，请刷新后重试')
            if kind == 'workspace_entries' and old:
                if expected_version is None or any(old.get(k) != data.get(k) for k in ('workspace_id', 'form_key')):
                    raise ValueError('专项表单已更新或类型不符，请刷新')
            def persist(target_id, value, previous):
                stamp = now()
                value = dict(value)
                value.pop('id', None)
                value['version'] = previous.get('version', 0) + 1 if previous else 1
                if kind == 'prompts':
                    if previous:
                        snapshot = dict(previous, prompt_id=target_id)
                        snapshot.pop('id', None)
                        db.execute('INSERT INTO objects VALUES(?,?,?)', ('prompt_history', uuid4().hex, json.dumps(snapshot, ensure_ascii=False)))
                else:
                    history = list(previous.get('oldhistory', [])) if previous else []
                    if previous:
                        history.append(dict({k: v for k, v in previous.items() if k != 'oldhistory'}, id=target_id))
                    value.update(oldhistory=history, created=previous['created'] if previous else stamp, updated=stamp)
                db.execute('INSERT INTO objects VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data', (kind, target_id, json.dumps(value, ensure_ascii=False)))
                self.audit(db, '保存' + kind, target_id)
                return dict(value, id=target_id)
            if prompt_defaults and kind == 'prompts' and data.get('purpose') == 'form_fill' and data.get('is_default') is True:
                others = list(db.execute("SELECT id,data FROM objects WHERE kind='prompts' AND id<>?", (oid,)))
                for other in others:
                    value = json.loads(other['data'])
                    if value.get('purpose') == 'form_fill' and value.get('module') == data.get('module') and value.get('is_default'):
                        persist(other['id'], dict(value, is_default=False), value)
            return persist(oid, data, old)

    def review_draft(self, oid, expected_revision, content, approved):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('必须提供有效草稿revision')
        if type(approved) is not bool:
            raise ValueError('approved必须为布尔值')
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT data FROM objects WHERE kind='drafts' AND id=?", (oid,)).fetchone()
            if not row:
                raise ValueError('草稿不存在')
            draft = json.loads(row['data'])
            if draft.get('revision', 1) != expected_revision:
                raise ValueError('草稿已更新，请刷新')
            if content is None:
                content = draft.get('content')
            if not isinstance(content, str) or not content.strip():
                raise ValueError('草稿正文不能为空')
            revision = expected_revision + 1
            draft.update(content=content, status='approved' if approved else 'draft',
                         reviewed=now(), revision=revision,
                         approved_revision=revision if approved else None)
            db.execute("UPDATE objects SET data=? WHERE kind='drafts' AND id=?", (json.dumps(draft, ensure_ascii=False), oid))
            self.audit(db, '批准草稿' if approved else '保存草稿', oid, 'revision:' + str(revision))
            return dict(draft, id=oid)

    def claim_approved_delivery(self, draft_id, channel_id, request_id, expected_revision):
        """Linearization point for approval, channel eligibility and immutable send snapshot.

        A later revocation does not cancel an already claimed snapshot; revoking
        first prevents this claim. Never re-read live draft content for delivery.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('必须提供有效草稿revision')
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 100:
            raise ValueError('发送必须提供16—100位幂等请求ID')
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT id FROM deliveries WHERE schedule_id='manual' AND slot=?", (request_id,)).fetchone():
                return None
            snapshots = []
            for kind, oid in (('drafts', draft_id), ('channels', channel_id)):
                row = db.execute('SELECT data FROM objects WHERE kind=? AND id=?', (kind, oid)).fetchone()
                if not row:
                    raise ValueError('渠道或草稿不存在')
                snapshots.append(dict(json.loads(row['data']), id=oid))
            draft, channel = snapshots
            revision = draft.get('revision', 1)
            if type(revision) is not int or revision != expected_revision:
                raise ValueError('草稿已更新，请刷新')
            if draft.get('status') != 'approved' or type(draft.get('approved_revision')) is not int or draft['approved_revision'] != revision:
                raise ValueError('草稿须先审核批准当前版本')
            if channel.get('enabled', True) is not True:
                raise ValueError('渠道已停用')
            if not isinstance(draft.get('content'), str) or not draft['content'].strip():
                raise ValueError('草稿正文不能为空')
            oid = uuid4().hex
            snapshot = {'draft_id': draft_id, 'channel_id': channel_id, 'request_id': request_id,
                        'revision': revision, 'approved_revision': revision,
                        'title': draft.get('title', ''), 'content': draft['content'],
                        'content_sha256': hashlib.sha256(draft['content'].encode('utf-8')).hexdigest(),
                        'claimed': now()}
            detail = dict(snapshot, snapshot=dict(snapshot))
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?)', (oid, 'manual', request_id, 'pending', json.dumps(detail, ensure_ascii=False)))
            self.audit(db, '创建发送任务', oid, 'revision:' + str(revision))
            return {'id': oid, 'draft': draft, 'channel': channel}

    def claim_delivery(self, schedule_id, slot, data):
        oid = uuid4().hex
        with self.lock, self.connect() as db:
            try:
                db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?)', (oid, schedule_id, slot, 'pending', json.dumps(data, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                return None
            self.audit(db, '创建发送任务', oid)
        return oid

    def delivery_state(self, oid, state, detail):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM deliveries WHERE id=?', (oid,)).fetchone()
            merged = json.loads(row['data']) if row else {}
            snapshot = merged.get('snapshot')
            merged.update(detail)
            if isinstance(snapshot, dict):
                # Delivery progress cannot overwrite the approved content snapshot.
                merged.update(snapshot)
                merged['snapshot'] = snapshot
            db.execute('UPDATE deliveries SET state=?,data=? WHERE id=?', (state, json.dumps(merged, ensure_ascii=False), oid))
            self.audit(db, '发送状态:' + state, oid)

    def deliveries(self):
        with self.connect() as db:
            return [unpack(r) for r in db.execute('SELECT * FROM deliveries ORDER BY rowid DESC LIMIT 500')]

    def audit_rows(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 500')]

    def backup(self, folder):
        target = Path(folder) / ('itmanager-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6] + '.sqlite3')
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.connect() as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
        return target.name

    @staticmethod
    def validate_backup(path):
        """Read-only offline restore preflight; NEVER replace a running database.

        Stop the service, retain the current database, then let the operator copy
        a validated backup. Prototype v0 backups may be migrated on next startup.
        """
        source = Path(path).resolve()
        if not source.is_file():
            raise ValueError('备份文件不存在')
        try:
            db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('备份完整性校验失败')
                required = {'records': {'id', 'module', 'data', 'version', 'created', 'updated'},
                            'objects': {'kind', 'id', 'data'}, 'audit': {'id', 'time', 'action', 'object_id', 'detail'},
                            'deliveries': {'id', 'schedule_id', 'slot', 'state', 'data'}}
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                version = (db.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0] or 0) if 'schema_versions' in tables else 0
                if not isinstance(version, int) or not 0 <= version <= SCHEMA_VERSION:
                    raise ValueError('不支持的备份数据库版本')
                if version:
                    required['records'].add('archived')
                    required['record_history'] = {'id', 'version', 'module', 'data', 'created', 'updated', 'archived'}
                for table, columns in required.items():
                    if not columns <= {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}:
                        raise ValueError('备份数据库结构无效')
                count = 0
                for module, data, record_version in db.execute('SELECT module,data,version FROM records'):
                    if module not in MODULES or not isinstance(json.loads(data), dict) or not isinstance(record_version, int) or record_version < 1:
                        raise ValueError('备份记录格式无效')
                    count += 1
                return {'valid': True, 'schema_version': version, 'records': count, 'offline_only': True}
            finally:
                db.close()
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise ValueError('备份文件不可读或格式无效') from None

    validate_restore = validate_backup
