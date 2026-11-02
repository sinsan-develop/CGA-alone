import base64,hashlib,hmac,importlib
import pytest
from fastapi import HTTPException

BODY=b'{"destination":"synthetic-only","events":[]}'
SECRET='synthetic-line-secret'

def signature(body=BODY,secret=SECRET):return base64.b64encode(hmac.new(secret.encode(),body,hashlib.sha256).digest()).decode()
def check(body=BODY,sig=None,secret=SECRET):return importlib.import_module('app.services.channel_connection_line_auth').verify_line_signature(body,signature() if sig is None else sig,secret)

def test_signature_uses_exact_original_bytes():
    assert check() is None
    with pytest.raises(HTTPException):check(BODY+b' ')
    different=b'{ "destination":"synthetic-only", "events":[] }'
    assert check(different,signature(different)) is None

@pytest.mark.parametrize('sig',['','not-base64','0'*64,signature()+'\n',signature().rstrip('='),signature()[:-2]+'AA',None,123])
def test_noncanonical_or_wrong_signature_is_rejected(sig):
    m=importlib.import_module('app.services.channel_connection_line_auth')
    with pytest.raises(HTTPException) as error:m.verify_line_signature(BODY,sig,SECRET)
    assert error.value.status_code==401 and error.value.detail=='LINE_AUTHENTICATION_FAILED'

@pytest.mark.parametrize('body,secret',[(BODY,'another-synthetic-secret'),(BODY,''),(BODY,None),(BODY,{}),('not-raw-bytes',SECRET),(b'x'*262145,SECRET)],ids=['wrong-secret','empty-secret','missing-secret','wrong-type-secret','not-raw','oversize'])
def test_invalid_raw_body_or_secret_never_authenticates(body,secret):
    with pytest.raises(HTTPException):check(body,signature(),secret)
