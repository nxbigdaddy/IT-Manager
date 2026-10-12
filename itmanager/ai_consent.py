"""Bind explicit client preview to the precise selected record snapshot."""
import hashlib,json

def records_hash(records):
    canonical=sorted(records,key=lambda r:r['id'])
    return hashlib.sha256(json.dumps(canonical,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()

def require_snapshot(records,expected):
    if records and expected!=records_hash(records):
        raise ValueError('选定资料已变化或缺少预览校验，请刷新后重新授权')
