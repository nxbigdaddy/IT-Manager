"""Configurable daily-checklist templates. Pure data: no IO, no network, no code execution.

A template is a declarative configuration:

    {'name', 'description', 'scope',
     'items': [{'name', 'hint', 'required'}], 'enabled', 'version'}

Every rejection message comes from SAFE_TEMPLATE_ERRORS verbatim, so no user input
is ever echoed into a validation message.
"""
from __future__ import annotations

from copy import deepcopy

SAFE_TEMPLATE_ERRORS = (
    '检查模板名称无效',
    '检查模板名称重复或为空',
    '检查模板说明过长',
    '检查模板适用范围无效',
    '检查项目须为1—50项',
    '检查项目名称无效或重复',
    '检查项目提示过长',
    '检查项目必填标记须为布尔值',
    '检查模板包含未知字段',
    '检查模板启用状态须为布尔值',
)
TEMPLATE_FIELDS = ('name', 'description', 'scope', 'items', 'enabled', 'version')
ITEM_FIELDS = ('name', 'hint', 'required')
NAME_LIMIT = 60
DESCRIPTION_LIMIT = 2000
SCOPE_LIMIT = 200
ITEM_LIMIT = 50
ITEM_NAME_LIMIT = 120
HINT_LIMIT = 1000
GENERIC_SCOPE = '通用'
BUILTIN_TEMPLATE_NAMES = ('备份运行', '入离职权限', '合同续费', '周度项目检查')

# Built from the original checkpoint wording; every hint states how "pass" is judged.
BUILTIN_TEMPLATES = (
    {'name': '备份运行', 'scope': 'recovery',
     'description': '按备份作业核对运行结果、覆盖范围与异地恢复可用性；仅在本机核查证据，不触发任何恢复操作。',
     'items': (
         {'name': '备份作业运行结果', 'hint': '通过判定：最近一次备份作业全部成功，无失败或长时间未运行记录，且保留可核对的作业日志。',
          'required': True},
         {'name': '备份覆盖范围与最近恢复点', 'hint': '通过判定：范围内系统均在备份清单内，最近恢复点满足既定频率与保留期，缺口已登记。',
          'required': True},
         {'name': '异地备份与恢复可用性', 'hint': '通过判定：存在异地或隔离副本，且有可追溯的恢复演练证据；仅计划未演练判为异常。',
          'required': True},
     )},
    {'name': '入离职权限', 'scope': 'access',
     'description': '核对入职授权依据、离职账号停用与高权限账号复核；只读检查，不改动任何账号。',
     'items': (
         {'name': '入职授权与审批依据', 'hint': '通过判定：账号开通有对应审批依据与责任人，权限范围与岗位职责一致。',
          'required': True},
         {'name': '离职账号停用及权限回收', 'hint': '通过判定：离职人员账号已停用、权限已回收，并保留回收时间与执行人证据。',
          'required': True},
         {'name': '高权限账号与MFA复核', 'hint': '通过判定：管理员与共享账号清单完整，多因素认证状态逐项复核且结论已记录。',
          'required': True},
     )},
    {'name': '合同续费', 'scope': 'contracts',
     'description': '在通知期限内核对到期、预算审批与供应商退出安排；只做记录，不对外发送任何通知。',
     'items': (
         {'name': '合同到期与通知期限', 'hint': '通过判定：到期日与通知截止日已核对，通知期限前留出决策时间。',
          'required': True},
         {'name': '续费预算及审批进度', 'hint': '通过判定：续费金额、预算科目与审批节点已确认，或已明确不再续费的决策依据。',
          'required': True},
         {'name': '供应商服务与退出安排', 'hint': '通过判定：服务水平与退出迁移安排已评估，数据与配置的移交路径清晰。',
          'required': True},
     )},
    {'name': '周度项目检查', 'scope': 'projects',
     'description': '每周核对里程碑进展、风险阻塞与下一步责任人；结论只作为管理参考。',
     'items': (
         {'name': '里程碑与本周进展', 'hint': '通过判定：里程碑完成情况有证据支撑，进度偏差已说明原因。',
          'required': True},
         {'name': '项目风险与阻塞', 'hint': '通过判定：风险与阻塞已登记到台账并指定责任人；未登记判为异常。',
          'required': True},
         {'name': '下一步行动与责任人', 'hint': '通过判定：下一步行动有明确内容、责任人与截止日期。',
          'required': True},
     )},
)


def _modules():
    from .schema import MODULES
    return MODULES


def module_names():
    """Module keys of the real record modules, the only scope references allowed."""
    return tuple(_modules())


def scope_choices():
    """(module key, Chinese label) pairs plus the generic scope for editors."""
    return [(key, caption) for key, (caption, _) in _modules().items()] + [('', GENERIC_SCOPE)]


def scope_label(value):
    """Chinese label for one stored scope value; unknown values pass through untrimmed."""
    value = value if isinstance(value, str) else ''
    return GENERIC_SCOPE if not value else _modules().get(value, (value, ()))[0]


def _scope_names():
    return set(_modules()) | {GENERIC_SCOPE}


def _text(value, limit):
    if not isinstance(value, str):
        return False
    if len(value) > limit:
        return False
    return not any(ord(char) < 32 and char not in '\n\r\t' for char in value)


def validate_template(data, existing=None, allowed=()):
    """Raise ValueError with a SAFE_TEMPLATE_ERRORS message when data is not usable.

    ``existing`` may list configured template names; repeating one is refused unless it
    is also named in ``allowed`` (the template's own former name while editing).
    Never echoes caller data back in the message. Returns None.
    """
    if not isinstance(data, dict):
        raise ValueError(SAFE_TEMPLATE_ERRORS[0])
    if set(data) - set(TEMPLATE_FIELDS):
        raise ValueError(SAFE_TEMPLATE_ERRORS[8])
    name = data.get('name')
    if not _text(name, NAME_LIMIT) or not name.strip():
        raise ValueError(SAFE_TEMPLATE_ERRORS[0])
    if name not in set(allowed or ()) and name in set(existing or ()):
        raise ValueError(SAFE_TEMPLATE_ERRORS[1])
    if 'description' in data and not _text(data.get('description'), DESCRIPTION_LIMIT):
        raise ValueError(SAFE_TEMPLATE_ERRORS[2])
    if 'scope' in data:
        scope = data.get('scope')
        # An empty scope means the same as 通用; everything else must name a real module.
        if (not isinstance(scope, str) or len(scope) > SCOPE_LIMIT
                or (scope.strip() and any(part.strip() not in _scope_names()
                                          for part in scope.split('、')))):
            raise ValueError(SAFE_TEMPLATE_ERRORS[3])
    if 'enabled' in data and type(data.get('enabled')) is not bool:
        raise ValueError(SAFE_TEMPLATE_ERRORS[9])
    if 'version' in data and (type(data.get('version')) is not int or data['version'] < 1):
        raise ValueError(SAFE_TEMPLATE_ERRORS[0])
    items = data.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= ITEM_LIMIT:
        raise ValueError(SAFE_TEMPLATE_ERRORS[4])
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) - set(ITEM_FIELDS):
            raise ValueError(SAFE_TEMPLATE_ERRORS[8])
        item_name = item.get('name')
        if not _text(item_name, ITEM_NAME_LIMIT) or not item_name.strip() or item_name in seen:
            raise ValueError(SAFE_TEMPLATE_ERRORS[5])
        seen.add(item_name)
        if 'hint' in item and not _text(item.get('hint'), HINT_LIMIT):
            raise ValueError(SAFE_TEMPLATE_ERRORS[6])
        if 'required' in item and type(item.get('required')) is not bool:
            raise ValueError(SAFE_TEMPLATE_ERRORS[7])


def default_templates():
    """Deep copies of the built-in templates, always at version 1."""
    result = []
    for spec in BUILTIN_TEMPLATES:
        result.append({
            'name': spec['name'],
            'description': spec['description'],
            'scope': spec['scope'],
            'items': [dict(item) for item in spec['items']],
            'enabled': True,
            'version': 1,
        })
    return result


def template_names(templates):
    """Template names in order, skipping unusable rows."""
    names = []
    for template in templates if isinstance(templates, (list, tuple)) else ():
        if isinstance(template, dict) and isinstance(template.get('name'), str):
            names.append(template['name'])
    return names


def item_names(template):
    """Item names in order for one template."""
    items = template.get('items') if isinstance(template, dict) else None
    if not isinstance(items, list):
        return []
    return [item['name'] for item in items
            if isinstance(item, dict) and isinstance(item.get('name'), str)]


def export_template(template):
    """Configuration only: never internal ids, versions, history or timestamps."""
    template = template if isinstance(template, dict) else {}
    items = template.get('items') if isinstance(template.get('items'), list) else []
    return {
        'name': template.get('name', ''),
        'description': template.get('description', ''),
        'scope': template.get('scope', ''),
        'enabled': bool(template.get('enabled', True)),
        'items': [
            {'name': item.get('name', ''),
             'hint': item.get('hint', ''),
             'required': bool(item.get('required', False))}
            for item in items if isinstance(item, dict)
        ],
    }
