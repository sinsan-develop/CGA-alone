"""LINE 原文 webhook 서명만 검증한다. 네트워크·DB·상태 변경 없음."""
from __future__ import annotations
import base64,binascii,hashlib,hmac
from fastapi import HTTPException


def verify_line_signature(body,signature,secret):
    try:
        if not isinstance(body,bytes) or len(body)>262144 or not isinstance(secret,str) or not secret.strip() or len(secret)>4096:raise ValueError()
        if not isinstance(signature,str) or len(signature)!=44:raise ValueError()
        digest=base64.b64decode(signature,validate=True)
        if len(digest)!=32 or base64.b64encode(digest).decode()!=signature:raise ValueError()
        expected=hmac.new(secret.encode('utf-8'),body,hashlib.sha256).digest()
        if not hmac.compare_digest(expected,digest):raise ValueError()
    except (ValueError,TypeError,UnicodeError,binascii.Error):raise HTTPException(401,'LINE_AUTHENTICATION_FAILED') from None
