"""Production entrypoint with bounded graceful shutdown and socket keepalive."""
import uvicorn

if __name__ == '__main__':
    uvicorn.run('app.main:app', host='0.0.0.0', port=8000,
        ws_ping_interval=15, ws_ping_timeout=20, timeout_graceful_shutdown=30,
        # Our structured middleware records route templates, never callback query secrets.
        access_log=False)
