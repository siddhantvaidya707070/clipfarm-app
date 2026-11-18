"""Run inside SupoClip backend with /legacy-secrets mounted read-only.

Uses native provider validation and encryption. Never prints credential values.
Original token files are never modified. No videos are uploaded by this command.
"""
import argparse
import asyncio
import json
from pathlib import Path
from sqlalchemy import text
from src.database import AsyncSessionLocal
from src.runtime_settings import encrypt_setting_value
from src.social.youtube import YouTubeProvider, SCOPES
from src.repositories.social_repository import SocialRepository


async def main(args):
    saved = json.loads((Path('/legacy-secrets') / f'youtube_{args.channel}.json').read_text())
    if not saved.get('refresh_token') or not set(SCOPES).issubset(set(saved.get('scope', '').split())):
        raise ValueError('Token needs reconnection: missing refresh access or required scopes')
    provider = YouTubeProvider()
    tokens = provider.refresh_tokens(saved['refresh_token'])
    profile = provider.fetch_profile(tokens.access_token)
    if profile.external_account_id != args.expected_channel:
        raise ValueError('Authenticated channel differs from intended destination; import aborted')
    async with AsyncSessionLocal() as db:
        owner = (await db.execute(text('SELECT id FROM users WHERE id=:id'), {'id': args.user})).scalar()
        if not owner:
            raise ValueError('Native owner does not exist')
        await SocialRepository.upsert_account(db, user_id=owner, provider='youtube',
            external_account_id=profile.external_account_id, display_name=profile.display_name,
            username=profile.username, avatar_url=profile.avatar_url,
            access_token_encrypted=encrypt_setting_value(tokens.access_token),
            refresh_token_encrypted=encrypt_setting_value(tokens.refresh_token),
            token_expires_at=tokens.expires_at, scopes=tokens.scopes or saved['scope'], metadata=profile.metadata)
        await db.commit()
    print('Native YouTube import verified: refreshed token, exact channel match, encrypted credentials saved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--channel', choices=['gaming', 'tjr'], required=True)
    parser.add_argument('--expected-channel', required=True)
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except Exception as exc:
        print('Native YouTube import failed:', type(exc).__name__, '(credentials not printed)')
        raise SystemExit(1)
