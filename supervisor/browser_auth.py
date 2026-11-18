"""Loopback browser sessions without exposing the service's bearer secret."""
import hashlib
import hmac
import os
import time
from urllib.parse import urlsplit
from fastapi import HTTPException

COOKIE = 'clipfarm_session'
MAX_AGE = 365 * 86400


def local_browser(request):
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        return False
    return request.headers.get('sec-fetch-site', 'none') in ('none', 'same-origin')


def loopback(request):
    return request.url.hostname in ('localhost', '127.0.0.1', '::1')


def session_value(expires=None):
    secret = os.getenv('CONTROL_TOKEN', '')
    if not secret:
        raise HTTPException(503, 'Local service credential is missing')
    value = str(int(expires or time.time() + MAX_AGE))
    signature = hmac.new(secret.encode(), ('browser-session:' + value).encode(), hashlib.sha256).hexdigest()
    return value + '.' + signature


def valid_session(value):
    try:
        expires, _ = value.split('.', 1)
        if int(expires) < time.time() or int(expires) > time.time() + MAX_AGE + 60:
            return False
        return hmac.compare_digest(value, session_value(int(expires)))
    except (ValueError, AttributeError, HTTPException):
        return False


def set_session(response):
    response.set_cookie(COOKIE, session_value(), max_age=MAX_AGE, httponly=True, samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return response
