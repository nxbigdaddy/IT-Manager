"""Allowlisted business errors suitable for desktop display, never raw upstream text."""
from .schema import MODULES

EXACT={
'选定资料已变化或缺少预览校验，请刷新后重新授权','AI变量必须为对象','最多选择100条记录',
'检查模板不存在','来源记录不存在','对象不存在','对象ID无效','提醒规则不存在',
'该检查模板已有历史检查记录，请改为停用',
'批量更新须为1—200条','批量更新项格式无效','批量更新记录ID无效','批量更新版本无效',
'批量更新数据无效','批量更新存在重复记录','批量归档须为1—200条','批量归档项格式无效',
'批量归档记录ID无效','批量归档版本无效','批量归档存在重复记录',
'名称不能为空','开始日期不能晚于截止日期','评分应在0—100之间','考核状态无效',
'完成任务需要完成证据和验收人','父任务不能形成环','父任务ID引用记录不存在',
'父子任务必须关联同一项目','记录不存在','记录已被更新，请刷新后重试',
'已定稿考核须先仅将状态转为待复核，再修改内容','记录仍被有效记录引用，不能归档',
'草稿不存在','草稿已更新，请刷新','草稿正文不能为空','渠道或草稿不存在',
'草稿须先审核批准当前版本','渠道已停用','模型已停用','请填写模型名称',
'请指定API密钥引用名称','请填写标题及正文','周期设置必须为对象','自动发送需有效渠道',
'SMTP主机必填','SMTP端口无效','收件人列表必填','邮箱地址无效','邮件必须使用TLS',
'机器人Webhook引用必填','凭据引用应为环境变量名，不能粘贴真实密钥或URL',
'AI 服务地址必须使用该提供商的官方 HTTPS 域名。','AI 服务地址无效。',
'提示词缺少必需变量。','提示词仅支持 {{variable}}，不支持表达式。',
'API密钥未配置，请先在系统设置中保存凭据。',
'请确认将表单资料发送给模型','模型或提示词无效/未启用','最多选择30条上下文记录',
'部分上下文记录已不存在，请刷新','人员、考核或权限信息需要二次授权',
'AI填表提示词必须指定业务模块','提示词用途无效',
'任务不存在','已完成或已取消任务不可执行管理动作','幂等请求ID已用于不同操作',
'进展状态只能为进行中','行动项须为1—20项列表','AI调用来源不存在','来源记录不存在',
'检查日期应为有效YYYY-MM-DD','检查项须为非空列表','检查结果无效','异常检查项必须填写note',
'专项资料已变化，请重新预览并授权','请确认将专项资料发送给模型','专项资料需要二次外发授权','专项资料过大，请减少关联记录后重试',
}
EXACT.update({'工作空间文本无效或超长','工作空间配置字段不符合白名单','工作空间配置列表数量无效','工作空间配置key或名称重复','工作空间配置key无效','工作空间日期须为YYYY-MM-DD','工作空间日期须为有效日期','工作空间模板超过128KiB','工作空间模板必须为有效JSON','工作空间模板schema_version必须为1','工作空间表单字段类型无效','只有choice字段允许options','工作空间提示词必须包含content且仅支持content/title变量','工作包引用的phase不存在','工作空间模板不存在','工作空间不存在','专项表单记录不存在','专项表单不存在','专项表单存在未知字段','专项表单状态无效','专项表单选项无效','专项表单数据超过128KiB','专项表单已更新或类型不符，请刷新','新增专项表单不能指定version','关联记录不存在或已归档','专项提示词不存在'})
FIELDS={field for _,fields in MODULES.values() for field in fields}

def user_message(message):
    if not isinstance(message,str):return None
    from .ai_forms import SAFE_FORM_ERRORS
    from .reminders import SAFE_REMINDER_ERRORS
    from .checklist_templates import SAFE_TEMPLATE_ERRORS
    from .reminder_state import SAFE_STATE_ERRORS
    from .health_state import SAFE_HEALTH_STATE_ERRORS
    from .batch_ops import SAFE_BATCH_ERRORS
    from .backup_plan import SAFE_BACKUP_ERRORS
    from .attachments import SAFE_ATTACHMENT_ERRORS
    from .backup_bundle import SAFE_BUNDLE_ERRORS
    from .global_search import SAFE_SEARCH_ERRORS
    from .tray_notifications import SAFE_TRAY_ERRORS
    if (message in EXACT or message in SAFE_FORM_ERRORS or message in SAFE_REMINDER_ERRORS
            or message in SAFE_TEMPLATE_ERRORS or message in SAFE_STATE_ERRORS
            or message in SAFE_HEALTH_STATE_ERRORS or message in SAFE_BATCH_ERRORS
            or message in SAFE_BACKUP_ERRORS or message in SAFE_ATTACHMENT_ERRORS
            or message in SAFE_BUNDLE_ERRORS or message in SAFE_SEARCH_ERRORS
            or message in SAFE_TRAY_ERRORS):return message
    for field in FIELDS:
        for suffix in ('应为有效的YYYY-MM-DD','应为有效ISO日期时间','应为非负十进制数值','引用记录不存在或模块不符'):
            if message==field+suffix:return message
    if message.startswith('必填字段不能为空: '):
        names=message.split(': ',1)[1].split(',')
        labels={'next_step':'下一步','reason':'原因','due_date':'新截止日期','approver':'批准人','evidence':'证据','acceptor':'验收人','name':'名称','date':'日期','result':'结果','note':'说明'}
        if names and all(n in labels for n in names):return '请填写：'+'、'.join(labels[n] for n in names)
    if message.startswith('请填写：') and all(n in {'下一步','原因','新截止日期','批准人','证据','验收人','名称','日期','结果','说明'} for n in message[4:].split('、')):return message
    prefix='考核定稿缺少: '
    if isinstance(message,str) and message.startswith(prefix):
        missing=message[len(prefix):].split('、')
        if missing and all(x in FIELDS for x in missing):return message
    return None
