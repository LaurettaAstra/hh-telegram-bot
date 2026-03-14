from sqlalchemy import text

from app.db import engine

if __name__ == "__main__":
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    print("DB connected")
