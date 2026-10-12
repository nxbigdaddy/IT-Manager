import os
import pytest
from itmanager.secrets import SecretVault

@pytest.mark.skipif(os.name!='nt',reason='DPAPI Windows only')
def test_dpapi_roundtrip(tmp_path,monkeypatch):
    key='ITM_SECRET_TEST_VAULT'
    monkeypatch.delenv(key,raising=False)
    vault=SecretVault(tmp_path/'vault.json')
    vault.save(key,'fake-sensitive-value-for-test')
    assert 'fake-sensitive-value-for-test' not in (tmp_path/'vault.json').read_text()
    monkeypatch.delenv(key)
    vault.load_environment()
    assert os.environ[key]=='fake-sensitive-value-for-test'
    assert vault.names()==[key]
    monkeypatch.delenv(key)

@pytest.mark.parametrize('name',['PATH','PYTHONPATH','ITM_ADMIN_TOKEN','bad-name','ITM_SECRET_'])
def test_disallow_process_variable_override(tmp_path,name):
    with pytest.raises(ValueError):SecretVault(tmp_path/'vault.json').save(name,'dummy')
