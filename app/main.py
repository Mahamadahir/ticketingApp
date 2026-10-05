from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.database import create_db_and_tables
from app.routers import tickets, webhooks

app = FastAPI(title="IT Support Ticketing")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(tickets.router)
app.include_router(webhooks.router)


@app.on_event("startup")
def on_startup() -> None:
    create_db_and_tables()


@app.get("/")
def root():
    return RedirectResponse(url="/tickets")
