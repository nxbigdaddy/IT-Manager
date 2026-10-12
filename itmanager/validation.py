"""Configuration validation before persistence (no outbound requests)."""
import re
from .ai import _endpoint,AIError

FIELDS={
 'providers':{'id','name','provider','model','base_url','api_key_env','temperature','max_tokens','timeout','enabled'},
 'channels':{'id','name','type','webhook_env','secret_env','host','port','security','username_env','password_env','from_addr','to_addrs','timeout','enabled'},
 'prompts':{'id','name','category','template','system','enabled','version','purpose','module','is_default'},
 'schedules':{'id','name','kind','mode','channel_id','owner','period_days','schedule','attachment','enabled','next_run','keep','scope'},
}

def strict_bools(data,keys):
    for key in keys:
        if key in data and type(data[key]) is not bool:raise ValueError(key+'必须为布尔值true/false')


def validate_config(kind,data):
    if set(data)-FIELDS[kind]:raise ValueError('不支持的配置字段：'+','.join(sorted(set(data)-FIELDS[kind])))
    strict_bools(data,('enabled','attachment','is_default'))
    if kind=='prompts':
        from .schema import MODULES
        if data.get('purpose','draft') not in ('draft','form_fill'):raise ValueError('提示词用途无效')
        if data.get('purpose')=='form_fill' and data.get('module') not in MODULES:raise ValueError('AI填表提示词必须指定业务模块')
    for key,value in data.items():
        if key.endswith('_env') and value and (not isinstance(value,str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,127}',value)):
            raise ValueError('凭据引用应为环境变量名，不能粘贴真实密钥或URL')
    if kind=='providers':
        provider=data.get('provider')
        if provider not in ('deepseek','kimi','qwen'):raise ValueError('不支持的模型平台')
        if data.get('base_url'):
            try:_endpoint(provider,data['base_url'])
            except AIError as exc:raise ValueError(str(exc))
        if not data.get('api_key_env'):raise ValueError('请指定API密钥引用名称')
        for key,low,high in [('temperature',0,2),('timeout',1,120),('max_tokens',1,1000000)]:
            value=data.get(key)
            if value is not None and (type(value) not in (int,float) or not low<=value<=high):raise ValueError(key+'超出范围')
    if kind=='channels':
        if data.get('type')=='email':
            if data.get('security','ssl') not in ('ssl','starttls'):raise ValueError('邮件必须使用TLS')
            if not isinstance(data.get('host'),str) or not data['host'].strip():raise ValueError('SMTP主机必填')
            if type(data.get('port',465)) is not int or not 1<=data.get('port',465)<=65535:raise ValueError('SMTP端口无效')
            if not isinstance(data.get('to_addrs'),list) or not data['to_addrs']:raise ValueError('收件人列表必填')
            for addr in [data.get('from_addr','')]+data['to_addrs']:
                if not isinstance(addr,str) or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',addr):raise ValueError('邮箱地址无效')
        elif not data.get('webhook_env'):raise ValueError('机器人Webhook引用必填')
    if kind=='schedules':
        if not isinstance(data.get('schedule'),dict):raise ValueError('周期设置必须为对象')
        if set(data['schedule'])-{'frequency','type','timezone','hour','minute','weekday','day'}:raise ValueError('周期包含未知字段')
