"""Spuštění web UI: python web.py"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.web.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
