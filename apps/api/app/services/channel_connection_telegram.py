"""Telegram 고정 Bot API와 읽기 전용 신원·채팅 권한 검증. 실제 실행 등록은 별도다."""
from __future__ import annotations
import asyncio,json,re
from dataclasses import dataclass
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
from fastapi import HTTPException

READ_METHODS=frozenset({'getMe','getWebhookInfo','getChat','getChatMember'})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


class TelegramApiRejected(Exception):
    def __init__(self,code,retry_after=None):self.code=code;self.retry_after=retry_after;super().__init__('TELEGRAM_API_REJECTED')


def _post(url,body):
    try:
        try:
            response=build_opener(_NoRedirect()).open(Request(url,data=body,headers={'Content-Type':'application/json','Accept':'application/json'},method='POST'),timeout=5)
        except HTTPError as error:response=error
        with response:
            raw=response.read(1048577)
            if len(raw)>1048576:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            if not 200<=response.status<300 and (value.get('ok') is not False or value.get('error_code')!=response.status):raise ValueError()
            return value
    except Exception:raise HTTPException(503,'TELEGRAM_TRANSPORT_FAILED') from None


class TelegramHttpTransport:
    def __init__(self,*,post=_post):self.post=post
    def call(self,method,token,payload):
        if method not in READ_METHODS or not isinstance(token,str) or not re.fullmatch('[1-9][0-9]{0,19}:[A-Za-z0-9_-]{1,2000}',token) or not isinstance(payload,dict):raise HTTPException(422,'TELEGRAM_CONFIGURATION_INVALID')
        raw=json.dumps(payload,ensure_ascii=False).encode()
        if len(raw)>1000000:raise HTTPException(422,'TELEGRAM_PACKET_LIMIT')
        value=self.post('https://api.telegram.org/bot'+token+'/'+method,raw)
        if not isinstance(value,dict):raise HTTPException(503,'TELEGRAM_TRANSPORT_FAILED')
        if value.get('ok') is not True:
            code=value.get('error_code');parameters=value.get('parameters');delay=parameters.get('retry_after') if isinstance(parameters,dict) else None
            if type(delay) is not int or not 0<=delay<=86400:delay=None
            if type(code) is int and code in {400,401,403,404,409,429}:raise TelegramApiRejected(code,delay) from None
            raise HTTPException(503,'TELEGRAM_TRANSPORT_FAILED')
        result=value.get('result')
        if not isinstance(result,dict):raise HTTPException(503,'TELEGRAM_TRANSPORT_FAILED')
        return result


def _id(value,*,positive=False):
    if type(value) is int:value=str(value)
    if not isinstance(value,str) or not re.fullmatch('-?[1-9][0-9]*',value) or abs(int(value))>4503599627370495 or positive and int(value)<1:raise HTTPException(422,'TELEGRAM_IDENTITY_INVALID')
    return value


@dataclass(frozen=True)
class TelegramIdentity:
    bot_user_id:str
    bot_username:str


@dataclass(frozen=True)
class TelegramChat:
    chat_id:str
    chat_type:str
    can_send_photos:bool


async def verify_bot_identity(http,token):
    try:
        value=await asyncio.to_thread(http.call,'getMe',token,{})
        bot=_id(value.get('id'),positive=True);username=value.get('username')
        if value.get('is_bot') is not True or not isinstance(username,str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{4,31}',username):raise ValueError()
        return TelegramIdentity(bot,username)
    except (ValueError,TypeError,AttributeError,TelegramApiRejected):raise HTTPException(403,'TELEGRAM_IDENTITY_VERIFICATION_FAILED') from None


async def verify_polling_available(http,token):
    try:
        value=await asyncio.to_thread(http.call,'getWebhookInfo',token,{})
        url=value.get('url');count=value.get('pending_update_count',0)
        if not isinstance(url,str) or type(count) is not int or count<0:raise ValueError()
        if url:raise HTTPException(409,'TELEGRAM_WEBHOOK_EXISTS')
        return count
    except (ValueError,TypeError,AttributeError,TelegramApiRejected):raise HTTPException(403,'TELEGRAM_IDENTITY_VERIFICATION_FAILED') from None


async def verify_chat_access(http,token,bot_user_id,chat_id):
    try:
        bot=_id(bot_user_id,positive=True);expected=_id(chat_id)
        chat=await asyncio.to_thread(http.call,'getChat',token,{'chat_id':expected})
        kind=chat.get('type')
        if _id(chat.get('id'))!=expected or kind not in {'private','group','supergroup'} or chat.get('is_direct_messages') is True or expected==bot:raise ValueError()
        if kind=='private':return TelegramChat(expected,kind,True)
        member=await asyncio.to_thread(http.call,'getChatMember',token,{'chat_id':expected,'user_id':int(bot)})
        user=member.get('user')
        if not isinstance(user,dict) or _id(user.get('id'),positive=True)!=bot or user.get('is_bot') is not True:raise ValueError()
        status=member.get('status')
        if status in {'creator','administrator'}:return TelegramChat(expected,kind,True)
        if status=='restricted':
            if member.get('is_member') is not True:raise ValueError()
            permissions=member
        elif status=='member':permissions=chat.get('permissions',{})
        else:raise ValueError()
        if not isinstance(permissions,dict) or permissions.get('can_send_messages') is not True:raise ValueError()
        return TelegramChat(expected,kind,permissions.get('can_send_photos') is True)
    except (ValueError,TypeError,AttributeError,TelegramApiRejected):raise HTTPException(403,'TELEGRAM_CHAT_VERIFICATION_FAILED') from None
