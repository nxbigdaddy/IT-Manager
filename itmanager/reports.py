from datetime import datetime
from .schema import MODULES


def build_report(records, start, end, owner='', modules=None):
    datetime.strptime(start,'%Y-%m-%d'); datetime.strptime(end,'%Y-%m-%d')
    if start>end: raise ValueError('开始日期不得晚于结束日期')
    selected=[]
    for r in records:
        if modules is not None and r['module'] not in modules: continue
        if r['module'] in ('people','reviews','access'): continue
        d=r['data']
        if owner and owner not in [d.get(k,'') for k in ('负责人','业务负责人','项目负责人','人员ID','提出人')]: continue
        selected.append(r)
    changed=[r for r in selected if start<=r['updated'][:10]<=end]
    overdue=[r for r in selected if r['data'].get('截止日期','') and r['data']['截止日期']<end and r['data'].get('状态') not in ('已完成','已取消','已关闭')]
    lines=[f'# IT工作汇报（草稿）',f'周期：{start} 至 {end}',f'负责人筛选：{owner or "全部"}', '',
           '> 口径：周期内更新记录＋截至结束日期的当前未结项；不是历史快照。更新时间按UTC日期；不包含人员考核、人员资料和权限明细。发送前请核实。','',f'## 周期内更新（{len(changed)}项）']
    for r in changed:
        d=r['data'];title=d.get(MODULES[r['module']][1][0],'')
        lines.append(f'- [{r["id"]}] {title}｜{d.get("状态",d.get("结论","未标状态"))}｜{d.get("负责人",d.get("项目负责人",""))}')
        for key in ('结果','完成证据','下一步','风险','结论'):
            if d.get(key):lines.append(f'  - {key}：{d[key]}')
    lines+=['',f'## 当前逾期未结（{len(overdue)}项）']
    for r in overdue:lines.append(f'- [{r["id"]}] {next(iter(r["data"].values()),"")}；截止 {r["data"]["截止日期"]}')
    lines+=['','## 需要决策 / 下期计划','请结合决策台账、风险及任务补充；未填写不代表没有问题。']
    return '\n'.join(lines)
