"""Single-administrator API transport. No credential persistence or logging."""
from __future__ import annotations
import ipaddress
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class ClientError(Exception):
    """Safe message; never includes remote response bodies or transport errors."""


def validate_url(value: str) -> str:
    try:
        u = urllib.parse.urlsplit(value)
        local = u.hostname == 'localhost'
        if not local:
            try:
                local = ipaddress.ip_address(u.hostname or '').is_loopback
            except ValueError:
                pass
        valid = (u.scheme in ('http', 'https') and u.hostname and
                 (u.scheme == 'https' or local) and u.username is None and
                 u.password is None and not u.query and not u.fragment and
                 '\\' not in value and not any(c.isspace() or ord(c) < 32 for c in value))
        _ = u.port
        if not valid:
            raise ValueError
    except (ValueError, TypeError):
        raise ClientError('服务地址无效：仅本机允许HTTP，远程必须使用HTTPS。') from None
    return value.rstrip('/')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class APIClient:
    def __init__(self, url, token, timeout=130):
        self.url = validate_url(url)
        if not isinstance(token, str) or len(token) < 24 or any(c.isspace() for c in token):
            raise ClientError('管理员令牌无效（至少24字符且不含空白）。')
        self._token = token
        self.timeout = timeout

    def __repr__(self):
        return '<APIClient credentials=redacted>'

    @classmethod
    def from_token_file(cls, url, token_file):
        try:
            token = Path(token_file).read_text(encoding='utf-8-sig').strip()
        except (OSError, UnicodeError):
            raise ClientError('无法读取令牌文件。') from None
        return cls(url, token)

    def request(self, method, path, body=None):
        if not path.startswith('/') or path.startswith('//') or '\\' in path:
            raise ClientError('无效API路径。')
        try:
            payload = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(self.url + path, data=payload, method=method,
                headers={'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json', 'Accept': 'application/json'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            with opener.open(req, timeout=self.timeout) as response:
                raw = response.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise ClientError('响应过大，请缩小范围。')
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            status = exc.code
            safe_message = None
            try:
                from .errors import user_message
                error_data=json.loads(exc.read(8192))
                if isinstance(error_data,dict):safe_message=user_message(error_data.get('user_message'))
            except Exception:pass
            finally:exc.close()
            messages = {400: '数据校验失败，请检查必填、关联、日期或配置。', 401: '认证失败，请检查令牌文件。',
                        404: '资源或接口不存在，请升级服务端。', 405: '服务端尚未支持此操作。',
                        409: '记录已被更新，请刷新后重新编辑。', 413: '提交数据过大。',
                        429: '请求过于频繁，请稍后操作。', 503: '外部服务暂不可用。'}
            raise ClientError(safe_message or messages.get(status, '服务请求失败，请检查服务状态。')) from None
        except ClientError:
            raise
        except Exception:
            raise ClientError('请求失败或响应无效，请检查网络；发送结果不确定时请先核对发送记录，不要直接重试。') from None

    def get(self, path):
        return self.request('GET', path)

    def post(self, path, body=None):
        return self.request('POST', path, body or {})


def ai_payload(records, provider_id, prompt_id, variables=None, sensitive_confirmed=False):
    """Inject both historical default {{content}} and {{records}} variables."""
    if not records or len(records) > 100:
        raise ClientError('请选择1—100条记录。')
    from .ai_consent import records_hash
    content = json.dumps(records, ensure_ascii=False, indent=2)
    values = dict(variables or {})
    values.update(content=content, records=content)
    return dict(provider_id=provider_id, prompt_id=prompt_id,
                record_ids=[r['id'] for r in records], context_hash=records_hash(records), variables=values,
                confirmed=True, sensitive_confirmed=sensitive_confirmed)


def export_data(path, payload):
    """Lossless JSON or Excel records; Excel cells explicitly remain text."""
    path = Path(path)
    if path.suffix.lower() != '.xlsx':
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    from openpyxl import Workbook
    from .schema import MODULES
    book = Workbook()
    meta = book.active
    meta.title = '导出信息'
    meta.append(['version', str(payload.get('version', 1))])
    meta.append(['exported', str(payload.get('exported', ''))])
    for module, (title, fields) in MODULES.items():
        sheet = book.create_sheet(title)
        sheet.append(['id', 'version', 'module'] + fields + ['完整记录JSON'])
        for record in payload.get('records', []):
            if record['module'] != module:
                continue
            values = [record['id'], record.get('version', ''), module]
            values += [record['data'].get(f, '') for f in fields]
            values += [json.dumps(record, ensure_ascii=False)]
            sheet.append([str(v) for v in values])
            for cell in sheet[sheet.max_row]:
                cell.data_type = 's'
        sheet.freeze_panes = 'D2'
        sheet.auto_filter.ref = sheet.dimensions
    book.save(path)


LEGACY_SHEETS = {
 '01_90天计划':('plans','工作项'), '02信息收集':('collection','信息类别'),
 '03访谈纪要':('logs','流程/场景'), '04系统台账':('systems','系统名称'),
 '05资产台账':('assets','名称/型号'), '06权限复核':('access','账号标识(非密码)'),
 '07风险台账':('risks','风险描述及证据'), '08需求池':('requests','问题及证据'),
 '09项目台账':('projects','项目名称'), '10任务台账':('tasks','事项/交付物'),
 '11运维事件':('incidents','症状及业务影响'), '12变更记录':('changes','变更内容及原因'),
 '13供应商合同':('contracts','供应商'), '14预算费用':('budget','事项'),
 '15备份恢复':('recovery','备份范围'), '16决策记录':('decisions','待决策事项'),
 '17周报':('logs','内容类别'), '18指标记录':('metrics','指标名称')}
LEGACY_ALIASES = {
 '建议阶段':'周期','工作内容':'业务目标','通过/验收标准':'验收标准','计划开始日期':'开始日期','截止日':'截止日期',
 '信息类别':'类别','必须收集内容':'收集内容','实际对接人':'提供人','支撑文件/证据类型':'支撑文件','缺口/冲突':'缺口',
 '证据链接':'证据','依赖系统/接口ID':'依赖','数据等级及所有者':'数据等级','RTO目标(小时)':'RTO小时','RPO目标(小时)':'RPO小时',
 '关联系统ID':'系统ID','使用人/责任人':'责任人','维保到期日':'维保到期日期','账号标识(非密码)':'账号标识',
 '角色与权限摘要':'权限摘要','授权工单ID':'审批依据','高权限MFA状态':'MFA状态','最近复核日期':'复核日期',
 '关联对象ID':'关联ID','业务影响':'影响','风险等级(人工确认)':'等级','风险负责人':'负责人',
 '长期应对/关联任务ID':'长期措施','残余风险/例外理由':'残余风险','接受到期/复核日':'复核日期','关闭验证证据':'关闭证据',
 '部门/提出人':'部门','问题及证据':'问题证据','目标与验收口径':'目标','粗估工作量(人周)':'工作量','预估费用(元)':'预计费用',
 '依赖与替代方案':'依赖','决策人及理由':'决策依据','业务发起人':'业务负责人','当前关口':'阶段','预算(元)':'预算',
 '风险/阻塞':'风险','复盘结论/下一步':'收益复盘','下一步行动':'下一步','处置负责人':'负责人',
 '症状及业务影响':'影响范围','处置过程/临时措施':'处置过程','用户确认及时间':'业务确认','根因/关联问题任务ID':'根因',
 '备份/恢复点':'备份','回退判据/步骤/时限':'回退判据及步骤','验证人/证据':'验证证据',
 '服务/系统范围':'服务范围','服务负责人':'负责人','SLA摘要':'SLA','合同金额':'金额','到期日':'到期日期',
 '最迟续约/解约通知日':'通知截止日期','数据归属/退出条款摘要':'退出安排',
 '关联项目/合同ID':'关联ID','已发生金额':'已发生','已承诺未发生金额':'已承诺未发生','预计新增金额':'预计新增','币种/税口径':'币种','财务确认人':'财务确认',
 '业务/技术负责人':'负责人','频率/保留期':'频率及保留','隔离/异地策略摘要':'隔离策略',
 '实际恢复耗时(小时)':'实际耗时','实际数据缺口(小时)':'实际数据缺口','完整性/业务验证证据':'证据','演练结论':'结论',
 '可选方案及影响':'方案对比','建议方案及依据':'建议','费用/资源请求':'资源请求','最迟决策日期':'截止日期','决策结论/例外期限':'结论',
 '计算口径':'口径','范围/系统ID':'范围','来源/证据链接':'证据','基线值':'基线','目标值':'目标','实际值':'实际',
 '结果/问题/请求':'工作内容','来源ID及证据链接':'证据','周期开始日期':'日期'}


def import_data(path):
    path = Path(path)
    if path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=False)
        records = []
        try:
            for sheet in book:
                rows = sheet.iter_rows(values_only=True)
                header = next(rows, ())
                from .schema import MODULES
                legacy = LEGACY_SHEETS.get(sheet.title)
                module = legacy[0] if legacy else next((key for key, (title, _) in MODULES.items() if sheet.title in (key, title)), None)
                if not module and 'module' not in header:
                    if sheet.title in ('00使用说明','19填写示例','导出信息'): continue
                    if any(any(v is not None for v in row) for row in rows):
                        raise ClientError('存在无法映射的工作表：'+sheet.title+'；请按模块名称重命名后导入。')
                    continue
                for row in rows:
                    if not any(value is not None for value in row): continue
                    cells = dict(zip(header, row))
                    original_cells = dict(cells)
                    if legacy:
                        cells['id'] = row[0]
                        cells['名称'] = cells.get(legacy[1]) or row[0] or sheet.title
                        for old,new in LEGACY_ALIASES.items():
                            if old in original_cells and new in MODULES[module][1] and new not in cells:
                                cells[new] = original_cells[old]
                        if module=='tasks' and original_cells.get('证据链接'): cells['完成证据']=original_cells['证据链接']
                        if module=='logs' and sheet.title=='03访谈纪要':
                            cells['工作内容']='\n'.join(f'{k}：{v}' for k,v in original_cells.items() if v is not None)
                    record = json.loads(cells['完整记录JSON']) if cells.get('完整记录JSON') else {}
                    target = cells.get('module') or module or record.get('module')
                    if target not in MODULES: raise ClientError('工作表模块无效。')
                    data = dict(record.get('data', {}))
                    for field in MODULES[target][1]:
                        if field in cells:
                            value = cells[field]
                            if hasattr(value, 'isoformat'): value = value.isoformat()[:10]
                            if value is not None or field in data:
                                data[field] = '' if value is None else str(value)
                    if legacy:
                        # Preserve source columns not represented in the new schema as readable provenance.
                        used = set(MODULES[target][1]) | {legacy[1],header[0]}
                        used |= {old for old,new in LEGACY_ALIASES.items() if new in MODULES[target][1]}
                        extras = [f'{k}：{v}' for k,v in original_cells.items() if k not in used and v is not None]
                        if extras:
                            destination = next((f for f in ('备注','工作内容','证据','风险','收益复盘','完成证据','关闭证据','验证证据','审批依据') if f in MODULES[target][1]), MODULES[target][1][-1])
                            data[destination] = (data.get(destination,'')+'\n原工作台补充字段：\n'+'\n'.join(extras)).strip()
                    record.update(module=target, data=data)
                    for key in ('id', 'version'):
                        if cells.get(key) is not None: record[key] = int(cells[key]) if key=='version' else str(cells[key])
                    records.append(record)
        finally:
            book.close()
        if not records:
            raise ClientError('Excel工作表名需为模块中文名称或英文键，第一行为schema字段名。')
        return {'records': records}
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('version', 1) != 1 or not isinstance(data.get('records'), list):
        raise ClientError('导入格式或版本无效。')
    return {'records': data['records']}
