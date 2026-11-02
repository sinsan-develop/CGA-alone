"""Google Chat HTTP앱 Project Number JWT 경계. 실제 수신 등록·호출은 별도다."""
from __future__ import annotations
from dataclasses import dataclass,field
import re,json,time,threading
from urllib.request import Request,build_opener,HTTPRedirectHandler
import jwt
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import HTTPException

ISSUER = 'chat@system.gserviceaccount.com'
CERTS_URL='https://www.googleapis.com/service_accounts/v1/metadata/x509/'+ISSUER


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


def google_public_certs(url):
    if url!=CERTS_URL:raise HTTPException(503,'GOOGLE_CHAT_SIGNING_KEYS_UNAVAILABLE')
    try:
        with build_opener(_NoRedirect()).open(Request(url,headers={'Accept':'application/json'}),timeout=5) as response:
            raw=response.read(1048577)
            if response.status!=200 or len(raw)>1048576:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            return value
    except Exception:raise HTTPException(503,'GOOGLE_CHAT_SIGNING_KEYS_UNAVAILABLE') from None


@dataclass(frozen=True)
class VerifiedGoogleChatRequest:
    project_number:str
    claims:dict=field(repr=False)


class GoogleChatAuthenticator:
    def __init__(self,*,transport=google_public_certs):
        self.transport=transport;self._keys={};self._loaded_at=0.;self._lock=threading.Lock()
    def _load(self,*,refresh=False):
        with self._lock:
            now=time.monotonic()
            if self._keys and now-self._loaded_at<300 and (not refresh or now-self._loaded_at<60):return dict(self._keys)
            try:
                keys=self.transport(CERTS_URL)
                if not isinstance(keys,dict) or not 1<=len(keys)<=100:raise ValueError()
                if any(not isinstance(k,str) or not 0<len(k)<=200 or not isinstance(v,str) or len(v)>32768 for k,v in keys.items()):raise ValueError()
            except Exception:raise HTTPException(503,'GOOGLE_CHAT_SIGNING_KEYS_UNAVAILABLE') from None
            self._keys=keys;self._loaded_at=now;return dict(keys)
    def authenticate(self,authorization,project_number):
        try:
            if not isinstance(project_number,str) or not re.fullmatch('[1-9][0-9]{0,19}',project_number):raise ValueError()
            if not isinstance(authorization,str) or len(authorization)>20000:raise ValueError()
            parts=authorization.split(' ')
            if len(parts)!=2 or parts[0].lower()!='bearer' or not parts[1]:raise ValueError()
            token=parts[1];header=jwt.get_unverified_header(token)
            if header.get('alg')!='RS256' or not isinstance(header.get('kid'),str) or not 0<len(header['kid'])<=200:raise ValueError()
            keys=self._load();pem=keys.get(header['kid'])
            if pem is None:pem=self._load(refresh=True).get(header['kid'])
            if pem is None:raise ValueError()
            public=x509.load_pem_x509_certificate(pem.encode()).public_key()
            if not isinstance(public,RSAPublicKey) or public.key_size<2048:raise ValueError()
            claims=jwt.decode(token,public,algorithms=['RS256'],issuer=ISSUER,audience=project_number,leeway=60,options={'require':['iss','aud','iat','exp'],'strict_aud':True})
            if type(claims['iat']) is not int or type(claims['exp']) is not int or claims['exp']<=claims['iat']:raise ValueError()
            return VerifiedGoogleChatRequest(project_number,claims)
        except HTTPException:raise
        except (jwt.PyJWTError,ValueError,TypeError,KeyError):raise HTTPException(401,'GOOGLE_CHAT_AUTHENTICATION_FAILED') from None
