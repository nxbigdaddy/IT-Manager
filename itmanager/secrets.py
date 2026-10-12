"""Windows DPAPI credential storage, bound to the service account.

Other systems use externally provisioned environment variables. There is no
plaintext fallback and no custom encryption scheme.
"""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import threading


class SecretVault:
    def __init__(self, path):
        self.path=Path(path)
        self.lock=threading.RLock()

    @staticmethod
    def validate_name(name):
        if not isinstance(name,str) or not re.fullmatch(r'(?:ITM_SECRET_|DEEPSEEK_|MOONSHOT_|DASHSCOPE_)[A-Z0-9_]{1,90}',name):
            raise ValueError('密钥变量名需以ITM_SECRET_、DEEPSEEK_、MOONSHOT_或DASHSCOPE_开头，仅含大写字母数字下划线')

    def _read(self):
        if not self.path.exists():return {}
        return json.loads(self.path.read_text(encoding='utf-8'))

    def names(self):return sorted(self._read())

    def _protect(self,data,decrypt=False):
        if os.name!='nt':raise ValueError('此系统请使用服务环境变量配置凭据；GUI密钥保存仅支持Windows DPAPI')
        class BLOB(ctypes.Structure):
            _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_byte))]
        buf=ctypes.create_string_buffer(data)
        src=BLOB(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte)));dest=BLOB()
        crypt=ctypes.windll.crypt32
        fn=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        fn.argtypes=[ctypes.POINTER(BLOB),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(BLOB)]
        fn.restype=wintypes.BOOL
        if not fn(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dest)):
            raise ValueError('系统凭据保护失败，请使用同一Windows服务账号或环境变量')
        try:return ctypes.string_at(dest.pbData,dest.cbData)
        finally:
            free=ctypes.windll.kernel32.LocalFree;free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
            free(dest.pbData)

    def save(self,name,value):
        self.validate_name(name)
        if not isinstance(value,str) or not value or len(value)>16384:raise ValueError('凭据不能为空或过长')
        encrypted=base64.b64encode(self._protect(value.encode('utf-8'))).decode('ascii')
        with self.lock:
            data=self._read();data[name]=encrypted
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(data),encoding='utf-8');temp.replace(self.path)
            os.environ[name]=value

    def load_environment(self):
        for name,value in self._read().items():
            self.validate_name(name)
            os.environ[name]=self._protect(base64.b64decode(value),decrypt=True).decode('utf-8')
