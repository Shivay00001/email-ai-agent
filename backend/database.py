import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

# PostgreSQL Connection Pooling for production
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./email.db")

engine = create_async_engine(
    DATABASE_URL, 
    echo=False,
    pool_size=10, 
    max_overflow=20,
    pool_recycle=3600
)
SessionLocal = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

async def get_db():
    async with SessionLocal() as session:
        yield session
