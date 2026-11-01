from importlib import import_module
from datetime import datetime,timezone
from uuid import uuid4
import json
import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException


def subject():return import_module('app.services.channel_connection_teams_auth')


@pytest.fixture
def signed():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);client=str(uuid4());now=int(datetime.now(timezone.utc).timestamp())
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()));jwk.update(kid='c03-synthetic-key',alg='RS256',use='sig',endorsements=['msteams'])
    claims={'iss':'https://api.botframework.com','aud':client,'nbf':now-30,'exp':now+600,'serviceurl':'https://smba.trafficmanager.net/amer/'}
    activity={'channelId':'msteams','serviceUrl':claims['serviceurl']}
    calls=[]
    def transport(url):
        calls.append(url)
        if url==subject().OPENID_URL:return {'issuer':claims['iss'],'jwks_uri':subject().KEYS_URL,'id_token_signing_alg_values_supported':['RS256']}
        if url==subject().KEYS_URL:return {'keys':[jwk]}
        raise AssertionError('unexpected external URL')
    def token(changes=None,**options):return jwt.encode({**claims,**(changes or {})},key,algorithm=options.get('algorithm','RS256'),headers={'kid':options.get('kid',jwk['kid'])})
    return key,client,jwk,claims,activity,calls,transport,token


def test_signed_microsoft_request_matches_exact_app_service_and_channel(signed):
    key,client,jwk,claims,activity,calls,transport,token=signed;m=subject();auth=m.TeamsAuthenticator(transport=transport)
    result=auth.authenticate('Bearer '+token(),activity,client)
    assert result.client_id==client and result.service_url==activity['serviceUrl']
    auth.authenticate('bearer '+token(),activity,client)
    assert calls==[m.OPENID_URL,m.KEYS_URL]


@pytest.mark.parametrize('change',[{'iss':'https://attacker.invalid'},{'aud':'wrong-app'},{'exp':1},{'nbf':9999999999},{'exp':None},{'serviceurl':'https://smba.trafficmanager.net/emea/'},{'serviceurl':None}])
def test_invalid_signed_claims_are_rejected_without_raw_token(signed,change):
    key,client,jwk,claims,activity,calls,transport,token=signed;raw=token(change)
    with pytest.raises(HTTPException) as error:subject().TeamsAuthenticator(transport=transport).authenticate('Bearer '+raw,activity,client)
    assert error.value.status_code in {401,403} and raw not in str(error.value.detail)


@pytest.mark.parametrize('header',['','Basic secret','Bearer invalid','Bearer a.b.c','Bearer a.b.c extra'])
def test_bad_authorization_does_not_bypass_verification(signed,header):
    key,client,jwk,claims,activity,calls,transport,token=signed
    with pytest.raises(HTTPException):subject().TeamsAuthenticator(transport=transport).authenticate(header,activity,client)


def test_wrong_key_signature_and_channel_endorsement_are_rejected(signed):
    key,client,jwk,claims,activity,calls,transport,token=signed
    other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    raw=jwt.encode(claims,other,algorithm='RS256',headers={'kid':jwk['kid']})
    with pytest.raises(HTTPException):subject().TeamsAuthenticator(transport=transport).authenticate('Bearer '+raw,activity,client)
    jwk['endorsements']=['webchat']
    with pytest.raises(HTTPException) as error:subject().TeamsAuthenticator(transport=transport).authenticate('Bearer '+token(),activity,client)
    assert error.value.status_code==403


@pytest.mark.parametrize('url',['http://smba.trafficmanager.net/amer/','https://smba.trafficmanager.net.attacker.invalid/amer/','https://user:password@smba.trafficmanager.net/amer/','https://smba.trafficmanager.net:444/amer/','https://127.0.0.1/','https://169.254.169.254/','https://smba.trafficmanager.net/amer/?redirect=secret','https://smba.trafficmanager.net/amer/#secret','https://smba.trafficmanager.net/%2e%2e/','https://smba.trafficmanager.net/../'])
def test_reply_destination_cannot_exfiltrate_tokens(url):
    with pytest.raises(HTTPException):subject().validate_teams_service_url(url)


def test_metadata_cannot_redirect_jwks_to_arbitrary_host(signed):
    key,client,jwk,claims,activity,calls,transport,token=signed
    def unsafe(url):return {'issuer':claims['iss'],'jwks_uri':'https://attacker.invalid/keys','id_token_signing_alg_values_supported':['RS256']}
    with pytest.raises(HTTPException):subject().TeamsAuthenticator(transport=unsafe).authenticate('Bearer '+token(),activity,client)


def test_non_teams_activity_is_rejected_before_loading_keys(signed):
    key,client,jwk,claims,activity,calls,transport,token=signed
    with pytest.raises(HTTPException):subject().TeamsAuthenticator(transport=transport).authenticate('Bearer '+token(),{**activity,'channelId':'webchat'},client)
    assert not calls


def test_hmac_algorithm_and_untrusted_key_urls_cannot_select_a_key(signed):
    key,client,jwk,claims,activity,calls,transport,token=signed
    raw=jwt.encode(claims,'synthetic-untrusted-key',algorithm='HS256',headers={'kid':jwk['kid'],'jku':'https://attacker.invalid/keys'})
    with pytest.raises(HTTPException):subject().TeamsAuthenticator(transport=transport).authenticate('Bearer '+raw,activity,client)
    assert not calls


def test_conflicting_service_claims_and_multi_audience_are_rejected(signed):
    key,client,jwk,claims,activity,calls,transport,token=signed
    for changes in [{'serviceUrl':'https://smba.trafficmanager.net/emea/'},{'aud':[client,'other']},{'exp':True}]:
        with pytest.raises(HTTPException):subject().TeamsAuthenticator(transport=transport).authenticate('Bearer '+token(changes),activity,client)


def test_unknown_key_id_is_throttled_instead_of_fetching_for_every_request(signed):
    key,client,jwk,claims,activity,calls,transport,token=signed;auth=subject().TeamsAuthenticator(transport=transport)
    auth.authenticate('Bearer '+token(),activity,client)
    for _ in range(3):
        with pytest.raises(HTTPException):auth.authenticate('Bearer '+token(kid='unknown-key'),activity,client)
    assert len(calls)==2
