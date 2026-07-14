from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database import init_db
from .api import auth, chat, jobs, results

app = FastAPI(title="DevGuard AI", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_db()

app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(results.router, prefix="/api")

@app.get("/")
def root():
    return {"message": "DevGuard AI API"}

@app.get("/health")
def health():
    return {"status": "healthy"}