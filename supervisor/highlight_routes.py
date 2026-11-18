"""Signed native routes; the existing Next.js session proxy owns browser access."""
import os

from fastapi import Depends, Request
from fastapi.responses import FileResponse


def install_highlight_routes(app, owner_dependency):
    def service(request):
        return request.app.state.highlights

    @app.get('/native/highlights')
    def list_streams(request: Request, user: str = Depends(owner_dependency)):
        return {'streams': service(request).list(user), 'config': {
            'model': os.getenv('OPENAI_TRANSCRIPTION_MODEL', 'gpt-transcribe'),
            'max_live_seconds': 10800, 'timing_quality': 'approximate',
        }}

    @app.post('/native/highlights')
    async def create(request: Request, user: str = Depends(owner_dependency)):
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError('Expected highlight settings')
        return service(request).create(user, data)

    @app.get('/native/highlights/{ident}')
    def details(ident: str, request: Request, user: str = Depends(owner_dependency)):
        value = service(request)
        return value.public(value.get(user, ident))

    @app.post('/native/highlights/{ident}/retry')
    def retry(ident: str, request: Request, user: str = Depends(owner_dependency)):
        return service(request).retry(user, ident)

    @app.post('/native/highlights/{ident}/stop')
    def stop(ident: str, request: Request, user: str = Depends(owner_dependency)):
        return service(request).stop(user, ident)

    @app.post('/native/highlights/{ident}/clips')
    async def add(ident: str, request: Request, user: str = Depends(owner_dependency)):
        return service(request).add_clip(user, ident, await request.json())

    @app.patch('/native/highlights/{ident}/clips/{clip_id}')
    async def edit(ident: str, clip_id: str, request: Request, user: str = Depends(owner_dependency)):
        return service(request).edit_clip(user, ident, clip_id, await request.json())

    @app.delete('/native/highlights/{ident}/clips/{clip_id}')
    def delete(ident: str, clip_id: str, request: Request, user: str = Depends(owner_dependency)):
        return service(request).delete_clip(user, ident, clip_id)

    @app.post('/native/highlights/{ident}/merge')
    async def merge(ident: str, request: Request, user: str = Depends(owner_dependency)):
        return service(request).merge(user, ident, (await request.json()).get('clip_ids'))

    @app.post('/native/highlights/{ident}/export')
    async def export(ident: str, request: Request, user: str = Depends(owner_dependency)):
        data = await request.json() if request.headers.get('content-type', '').startswith('application/json') else {}
        return service(request).export(user, ident, data.get('clip_ids'))

    @app.get('/native/highlights/{ident}/manifest')
    def manifest(ident: str, request: Request, user: str = Depends(owner_dependency)):
        value = service(request)
        value.get(user, ident)
        return FileResponse(value.folder(ident) / 'manifest.json', media_type='application/json', filename='manifest.json')

    @app.get('/native/highlights/{ident}/files/{filename}')
    def download(ident: str, filename: str, request: Request, user: str = Depends(owner_dependency)):
        path = service(request).file(user, ident, filename)
        return FileResponse(path, media_type='video/mp4' if path.suffix == '.mp4' else 'application/x-subrip', filename=path.name)
