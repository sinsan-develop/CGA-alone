import time
from datetime import datetime,timedelta,timezone
from importlib import import_module
import jwt,pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException


def subject():return import_module('app.services.channel_connection_google_chat_auth')


@pytest.fixture(scope='module')
def signing():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'synthetic-google-chat')]);now=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).sign(key,hashes.SHA256())
    return key,cert.public_bytes(serialization.Encoding.PEM).decode()


def claims():return dict(iss=subject().ISSUER,aud='1234567890',iat=int(time.time())-5,exp=int(time.time())+300)
def token(key,payload=None,**kw):return jwt.encode(payload or claims(),key,algorithm='RS256',headers={'kid':'synthetic-key'},**kw)


def test_google_signed_project_number_request_is_verified(signing):
    key,cert=signing;m=subject();calls=[]
    def transport(url):calls.append(url);return {'synthetic-key':cert}
    auth=m.GoogleChatAuthenticator(transport=transport);result=auth.authenticate('Bearer '+token(key),'1234567890')
    assert result.project_number=='1234567890' and result.claims['iss']==m.ISSUER and 'claims=' not in repr(result)
    auth.authenticate('Bearer '+token(key),'1234567890');assert calls==[m.CERTS_URL]


@pytest.mark.parametrize('bad',['issuer','audience','expired','future','missing','audience_list','boolean_time','signature','kid','algorithm'])
def test_untrusted_or_stale_requests_are_rejected(signing,bad):
    key,cert=signing;p=claims();header={'kid':'synthetic-key'}
    if bad=='issuer':p['iss']='untrusted.invalid'
    elif bad=='audience':p['aud']='9876543210'
    elif bad=='expired':p['exp']=int(time.time())-600;p['iat']=p['exp']-300
    elif bad=='future':p['iat']=int(time.time())+600;p['exp']=p['iat']+300
    elif bad=='missing':p.pop('iat')
    elif bad=='audience_list':p['aud']=['1234567890']
    elif bad=='boolean_time':p['iat']=True
    elif bad=='signature':key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    elif bad=='kid':header['kid']='other-key'
    value=jwt.encode(p,None,algorithm='none',headers=header) if bad=='algorithm' else jwt.encode(p,key,algorithm='RS256',headers=header)
    auth=subject().GoogleChatAuthenticator(transport=lambda _: {'synthetic-key':cert})
    with pytest.raises(HTTPException,match='GOOGLE_CHAT_AUTHENTICATION_FAILED'):auth.authenticate('Bearer '+value,'1234567890')


@pytest.mark.parametrize('authorization,project',[('Basic test','1234567890'),('Bearer test','https://untrusted.invalid'),('Bearer test','0123')])
def test_bad_request_shape_never_fetches_public_keys(authorization,project):
    def forbidden(_):raise AssertionError('public keys not needed')
    with pytest.raises(HTTPException):subject().GoogleChatAuthenticator(transport=forbidden).authenticate(authorization,project)
