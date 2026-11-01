from fastapi import APIRouter

from app.api.routes import channel_connections
from app.api.routes import admin, am, auth, bots, channels, edit_locks, hubs, richform, webchat


api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(bots.router)
api_router.include_router(hubs.router)
api_router.include_router(channels.router)
api_router.include_router(edit_locks.router)
api_router.include_router(am.router)
api_router.include_router(richform.router)
api_router.include_router(webchat.router)

api_router.include_router(channel_connections.router)
