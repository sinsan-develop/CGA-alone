"""Teams Bot Connector 경계. 설정/사용자 입력으로 JWT 검증을 끄거나 호스트를 넓히지 않는다."""
from __future__ import annotations
from dataclasses import dataclass,field
import json,re,threading,time
from urllib.parse import urlsplit,urlunsplit
from urllib.request import Request,build_opener,HTTPRedirectHandler
from uuid import UUID
import jwt
from fastapi import HTTPException

OPENID_URL='https://login.botframework.com/v1/.well-known/openidconfiguration'
KEYS_URL='https://login.botframework.com/v1/.well-known/keys'
ISSUER='https://api.botframework.com'
SERVICE_HOSTS=frozenset({'smba.trafficmanager.net','smba.infra.teams.microsoft.com'})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


def microsoft_public_json(url):
    # 이 함수에는 인증정보를 전달하지 않는다. metadata가 지시한 임의 host도 거부한다.
    if url not in {OPENID_URL,KEYS_URL}:raise HTTPException(503,'TEAMS_SIGNING_KEYS_UNAVAILABLE')
    try:
        with build_opener(_NoRedirect()).open(Request(url,headers={'Accept':'application/json'}),timeout=5) as response:
            raw=response.read(1048577)
            if response.status!=200 or len(raw)>1048576:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            return value
    except Exception:raise HTTPException(503,'TEAMS_SIGNING_KEYS_UNAVAILABLE') from None


def validate_teams_service_url(value):
    try:
        if not isinstance(value,str) or len(value)>1000 or '\\' in value:raise ValueError()
        parsed=urlsplit(value)
        if (parsed.scheme!='https' or parsed.hostname not in SERVICE_HOSTS or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None,443} or parsed.query or parsed.fragment or not re.fullmatch(r'/[A-Za-z0-9_/-]*/',parsed.path) or '..' in parsed.path or '//' in parsed.path):raise ValueError()
        return urlunsplit(('https',parsed.hostname,parsed.path,'',''))
    except (ValueError,TypeError):raise HTTPException(403,'TEAMS_DESTINATION_UNTRUSTED') from None


@dataclass(frozen=True)
class VerifiedTeamsRequest:
    client_id:str
    service_url:str
    claims:dict=field(repr=False)


class TeamsAuthenticator:
    def __init__(self,*,transport=microsoft_public_json):
        self.transport=transport;self._keys=[];self._loaded_at=0.;self._lock=threading.Lock()

    def _load(self,*,refresh=False):
        with self._lock:
            now=time.monotonic()
            if self._keys and now-self._loaded_at<3600 and (not refresh or now-self._loaded_at<60):return list(self._keys)
            try:
                metadata=self.transport(OPENID_URL)
                if metadata.get('issuer')!=ISSUER or metadata.get('jwks_uri')!=KEYS_URL or 'RS256' not in metadata.get('id_token_signing_alg_values_supported',[]):raise ValueError()
                keys=self.transport(KEYS_URL).get('keys')
                if not isinstance(keys,list) or not keys or len(keys)>100 or any(not isinstance(k,dict) for k in keys):raise ValueError()
            except Exception:raise HTTPException(503,'TEAMS_SIGNING_KEYS_UNAVAILABLE') from None
            self._keys=keys;self._loaded_at=now;return list(keys)

    def authenticate(self,authorization,activity,client_id):
        if not isinstance(activity,dict) or activity.get('channelId')!='msteams':raise HTTPException(403,'TEAMS_CHANNEL_FORBIDDEN')
        destination=validate_teams_service_url(activity.get('serviceUrl'))
        try:
            if not isinstance(client_id,str) or str(UUID(client_id))!=client_id:raise ValueError()
            if not isinstance(authorization,str) or len(authorization)>20000:raise ValueError()
            parts=authorization.split(' ')
            if len(parts)!=2 or parts[0].lower()!='bearer' or not parts[1]:raise ValueError()
            token=parts[1];header=jwt.get_unverified_header(token)
            if header.get('alg')!='RS256' or not isinstance(header.get('kid'),str) or not 0<len(header['kid'])<=200:raise ValueError()
            keys=self._load();matching=[k for k in keys if k.get('kid')==header['kid']]
            if not matching:
                matching=[k for k in self._load(refresh=True) if k.get('kid')==header['kid']]
            if len(matching)!=1:raise ValueError()
            key=matching[0]
            if key.get('kty')!='RSA' or key.get('alg','RS256')!='RS256' or key.get('use','sig')!='sig':raise ValueError()
            public=jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
            if public.key_size<2048:raise ValueError()
            claims=jwt.decode(token,public,algorithms=['RS256'],issuer=ISSUER,audience=client_id,leeway=300,
                options={'require':['iss','aud','nbf','exp'],'strict_aud':True})
            if type(claims['exp']) is not int or type(claims['nbf']) is not int:raise ValueError()
        except HTTPException:raise
        except (jwt.PyJWTError,ValueError,TypeError,KeyError):raise HTTPException(401,'TEAMS_AUTHENTICATION_FAILED') from None
        if not isinstance(key.get('endorsements'),list) or 'msteams' not in key['endorsements']:raise HTTPException(403,'TEAMS_CHANNEL_FORBIDDEN')
        services=[claims[name] for name in ('serviceurl','serviceUrl') if name in claims]
        if not services or any(validate_teams_service_url(value)!=destination for value in services):raise HTTPException(403,'TEAMS_SERVICE_URL_MISMATCH')
        return VerifiedTeamsRequest(client_id,destination,claims)
