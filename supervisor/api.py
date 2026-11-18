import hmac
import os
import secrets
import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool
from .browser_auth import COOKIE, local_browser, loopback, valid_session, set_session
from .store import Store
from .openai_gateway import forward, read_secret
from .rules import validate
from .ranges import validate_range
from .ingest import public_source
from .media_effects import normalize_effects, media_options
from .live_service import start_session, stop_session, list_sessions, get_session

store = None


@asynccontextmanager
async def lifespan(app):
    global store
    store = Store(os.getenv('SUPERVISOR_DATABASE_URL', 'sqlite:///clipfarm.db'))
    from .highlights import HighlightService
    app.state.highlights = HighlightService(store)
    app.state.highlights.recover()
    try:
        yield
    finally:
        app.state.highlights.close()


app = FastAPI(title='Clipfarm local control', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'supervisor'])


def native_owner(request: Request):
    user = request.headers.get('x-supoclip-user-id', '')
    stamp = request.headers.get('x-supoclip-ts', '')
    secret = os.getenv('BACKEND_AUTH_SECRET', '')
    try:
        valid_time = abs(time.time() - int(stamp)) <= 300
    except ValueError:
        valid_time = False
    expected = hmac.new(secret.encode(), f'{user}:{stamp}'.encode(), hashlib.sha256).hexdigest()
    if not user or not secret or not valid_time or not hmac.compare_digest(expected, request.headers.get('x-supoclip-signature', '')):
        raise HTTPException(401, 'Signed native session required')
    return user


from .highlight_routes import install_highlight_routes
install_highlight_routes(app, native_owner)


@app.get('/native/capture')
def native_captures(user: str = Depends(native_owner)):
    from .native_capture import sessions
    # Local paths and upload locators stay server-side.
    return [{k: v for k, v in r.items() if k not in ('folder', 'selected_path', 'upload_url', 'user_id')}
            for r in sessions(store, user)]


@app.post('/native/capture')
async def native_start(request: Request, user: str = Depends(native_owner)):
    from .native_capture import create_session
    row = await run_in_threadpool(create_session, store, user, await request.json())
    return {'id': row['id'], 'state': row['state']}


@app.post('/native/capture/{ident}/stop')
def native_stop(ident: str, user: str = Depends(native_owner)):
    from .native_capture import stop_session
    row = stop_session(store, user, ident)
    return {'id': row['id'], 'state': row['state']}


@app.middleware('http')
async def browser_guards(request, call_next):
    navigation = request.method == 'GET' and request.url.path in ('/', '/oauth/callback') and request.headers.get('sec-fetch-mode') == 'navigate' and request.headers.get('sec-fetch-dest') == 'document'
    if not local_browser(request) and not navigation:
        return JSONResponse({'detail': 'Open Clipfarm directly on localhost; cross-site access is blocked'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


bearer = HTTPBearer(auto_error=False)


def auth(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = os.getenv('CONTROL_TOKEN', '')
    if token and hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token):
        return
    if loopback(request) and valid_session(request.cookies.get(COOKIE, '')):
        return
    else:
        raise HTTPException(401, 'Local control token required. It is in clipfarm/.env.')


@app.exception_handler(ValueError)
async def validation_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=422)


@app.get('/health')
def health():
    return {'status': 'healthy', 'publishing': 'review-gated'}


@app.get('/', response_class=HTMLResponse)
def home(request: Request):
    response = HTMLResponse((Path(__file__).parent / 'control.html').read_text(encoding='utf-8'))
    return set_session(response) if loopback(request) else response


@app.get('/session')
def browser_session(request: Request):
    if not loopback(request):
        raise HTTPException(403, 'Browser sessions are available on localhost only')
    return set_session(JSONResponse({'connected': True, 'openai_configured': bool(read_secret('openai_key'))}))


@app.get('/status', dependencies=[Depends(auth)])
def status():
    clips = [{**{k:v for k,v in c.items() if k not in ('upload_session',)}, 'publish_block_reason': store.manual_block_reason(c['id'])} for c in store.list('clip')]
    return {'openai_configured': bool(read_secret('openai_key')), 'telegram_configured': bool(read_secret('telegram_token')), 'channels': store.list('channel'), 'jobs': store.list('job'), 'clips': clips, 'creators': store.list('creator'), 'usage': store.usage(), 'pilot_reviews': {k: store.review_count(k) for k in ('tjr', 'gaming')}, 'qualified': store.get('setting', 'qualification') or {'passed': False}, 'runtime': store.get('setting', 'heartbeat'), 'notifications': store.list('notification')[-20:]}


@app.post('/settings/secrets', dependencies=[Depends(auth)])
async def set_secret(request: Request):
    data = await request.json()
    for name in ('openai_key', 'telegram_token'):
        if name in data and isinstance(data[name], str):
            path = Path(os.getenv('SECRETS_DIR', '/secrets')) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data[name].strip(), encoding='utf-8')
    return {'saved': True}


@app.post('/creators', dependencies=[Depends(auth)])
async def creator(request: Request):
    data = await request.json()
    ident = data.get('id', '')
    if not ident or not ident.replace('-', '').isalnum() or data.get('channel') not in ('tjr', 'gaming'):
        raise HTTPException(422, 'Use an alphanumeric creator id and tjr/gaming channel')
    store.put('creator', ident, {**(store.get('creator', ident) or {}), **data})
    return {'saved': ident}


@app.post('/jobs', dependencies=[Depends(auth)])
async def queue(request: Request):
    data = await request.json()
    source = data.get('source', '')
    if not (public_source(source) or source.startswith('/data/inbox/')):
        raise HTTPException(422, 'Use an HTTPS source or an imported file in /data/inbox/')
    try:
        start, end = validate_range(data.get('start', data.get('source_offset', 0)), data.get('end', data.get('source_end')))
        creator = store.get('creator', data['creator'])
        if not creator:
            raise ValueError('Unknown creator')
        extra = {'source_offset': start, 'render_options': normalize_effects(data.get('effects'), creator.get('campaign', ''))}
        if end is not None:
            extra['source_end'] = end
        if data.get('original_source'):
            if not public_source(data['original_source']):
                raise ValueError('Original source must be a supported public URL')
            extra['original_source'] = data['original_source']
        return store.queue(data['creator'], source, **extra)
    except (ValueError, KeyError) as e:
        raise HTTPException(422, str(e))


@app.get('/media/options', dependencies=[Depends(auth)])
def render_options():
    return media_options()


@app.post('/live/start', dependencies=[Depends(auth)])
async def start_live(request: Request):
    data = await request.json()
    creator = store.get('creator', data.get('creator', ''))
    if not creator:
        raise HTTPException(422, 'Choose a creator')
    options = normalize_effects(data.get('effects'), creator.get('campaign', ''))
    return start_session(store, creator['id'], data.get('source', ''), data.get('start', 0), data.get('end'),
                         data.get('time_basis', 'recording'), render_options=options)


@app.get('/live/status', dependencies=[Depends(auth)])
def live_status():
    return {'sessions': [get_session(store, row['id']) for row in list_sessions(store)]}


@app.post('/live/{ident}/stop', dependencies=[Depends(auth)])
def stop_live(ident: str):
    return stop_session(store, ident)


@app.post('/jobs/{ident}/retry', dependencies=[Depends(auth)])
def retry(ident: str):
    row = store.get_job(ident)
    if not row or row['state'] not in ('held', 'error', 'retry'):
        raise HTTPException(409, 'Only a held or failed job can be retried')
    return store.update_job(ident, state='queued' if not row.get('engine_id') else 'processing', error='', next_attempt=0)


@app.post('/clips/{ident}/approve', dependencies=[Depends(auth)])
async def approve(ident: str, request: Request):
    checks = await request.json()
    clip = store.get('clip', ident)
    if not clip:
        raise HTTPException(404)
    creator = store.get('creator', store.get_job(clip['job_id'])['creator'])
    errors = validate(clip, creator.get('campaign', ''))
    if errors:
        raise HTTPException(422, errors)
    return store.review(ident, True, checks)


@app.post('/clips/{ident}/skip', dependencies=[Depends(auth)])
def skip(ident: str):
    return store.review(ident, False)


@app.patch('/clips/{ident}', dependencies=[Depends(auth)])
async def edit(ident: str, request: Request):
    data = await request.json()
    clip = store.get('clip', ident)
    if not clip or clip['state'] not in ('review', 'approved'):
        raise HTTPException(409, 'Only unpublished review clips can be edited')
    return store.update_clip(ident, **{k: v for k, v in data.items() if k in ('title', 'description')}, state='review', checks={})


@app.get('/clips/{ident}/file', dependencies=[Depends(auth)])
def clip_file(ident: str):
    clip = store.get('clip', ident)
    if not clip:
        raise HTTPException(404)
    path = Path(clip['path']).resolve()
    root = Path(os.getenv('DATA_DIR', '/data')).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='video/mp4')


@app.post('/channels/{ident}/pause', dependencies=[Depends(auth)])
async def pause(ident: str, request: Request):
    row = store.get('channel', ident)
    if not row:
        raise HTTPException(404)
    data = await request.json()
    if data.get('autopublish') and not store.autopublish_ready():
        raise HTTPException(409, 'Qualification and five reviewed clips per channel required')
    return store.put('channel', ident, {**row, 'paused': bool(data.get('paused', True)), 'autopublish': bool(data.get('autopublish', False))})


@app.post('/clips/{ident}/publish', dependencies=[Depends(auth)])
def publish(ident: str):
    try:
        return store.request_manual_publication(ident)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post('/v1/{endpoint:path}')
async def gateway(endpoint: str, request: Request):
    if endpoint not in ('chat/completions', 'responses'):
        raise HTTPException(404)
    expected = 'Bearer ' + os.getenv('GATEWAY_TOKEN', secrets.token_hex(32))
    if not hmac.compare_digest(request.headers.get('authorization', ''), expected):
        raise HTTPException(401)
    try:
        code, data = await forward(store, await request.json(), endpoint)
        return JSONResponse(data, status_code=code)
    except ValueError as e:
        return JSONResponse({'error': {'message': str(e)}}, status_code=429 if 'budget' in str(e) else 400)


@app.post('/tick', dependencies=[Depends(auth)])
def tick():
    store.put('setting', 'tick-requested', {'requested': True})
    return {'queued': True}


@app.put('/campaigns/{ident}', dependencies=[Depends(auth)])
async def configure_campaign(ident: str, request: Request):
    data = await request.json()
    if data.get('submission_verified') and not data.get('verification_receipt'):
        raise HTTPException(422, 'A supervised real submission receipt is required')
    if data.get('ai_editing_allowed') and not data.get('ai_permission_evidence'):
        raise HTTPException(422, 'Record the campaign organizer clarification first')
    return store.put('campaign', ident, {'id': ident, **data, 'browser_health_until': 0})


@app.post('/qualification', dependencies=[Depends(auth)])
async def qualify(request: Request):
    data = await request.json()
    runs = data.get('runs', [])
    required = {'talking-head', 'gameplay-facecam', 'gameplay-visual', 'recorded-live'}
    if {r.get('input') for r in runs} != required or not all(r.get('usable_clips', 0) >= 2 and r.get('restart_passed') and r.get('reviewed') and r.get('wall_seconds', 0) > 0 for r in runs):
        raise HTTPException(422, 'Four measured, reviewed corpus runs with restart recovery are required')
    return store.put('setting', 'qualification', {'passed': True, 'runs': runs})


@app.get('/oauth/{channel}/start', dependencies=[Depends(auth)])
def oauth_start(channel: str):
    from .youtube import authorization_url
    return {'url': authorization_url(store, channel)}


@app.get('/oauth/callback')
async def oauth_callback(code: str = '', state: str = ''):
    from .youtube import complete_oauth
    try:
        channel = await complete_oauth(store, code, state)
        return HTMLResponse('YouTube connected to ' + channel + '. Return to local controls; publishing remains paused.')
    except ValueError as e:
        raise HTTPException(400, str(e))
