from fastapi import Request

from app.db import DB


def get_db(request: Request) -> DB:
    return request.app.state.db
