from fastapi import Depends, FastAPI

#from .dependencies import get_query_token, get_token_header
#from .internal import admin
from .routers import youtube

app = FastAPI()


app.include_router(youtube.router)