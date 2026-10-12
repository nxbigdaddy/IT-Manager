import pytest
from itmanager.validation import validate_config,strict_bools

@pytest.mark.parametrize('key',['enabled','approved','confirmed','sensitive_confirmed','attachment'])
@pytest.mark.parametrize('value',['false','true',0,1,None])
def test_bool_is_not_truthiness(key,value):
    with pytest.raises(ValueError):strict_bools({key:value},[key])

@pytest.mark.parametrize('extra',[{'token':'secret'},{'url':'https://host?secret=x'},{'api_key_env':'sk-actual-key'},{'base_url':'https://api.deepseek.com/v1?key=secret'},{'base_url':'https://user:secret@api.deepseek.com/v1'}])
def test_credentials_rejected_before_persistence(extra):
    data={'provider':'deepseek','model':'deepseek-chat','api_key_env':'DEEPSEEK_API_KEY'}
    data.update(extra)
    with pytest.raises(ValueError):validate_config('providers',data)


def test_valid_provider():
    validate_config('providers',{'provider':'deepseek','model':'deepseek-chat','api_key_env':'DEEPSEEK_API_KEY','enabled':True})
