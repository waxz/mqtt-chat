import uvicorn
import asyncio
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

if __name__ == "__main__":
    # ws_ping_interval=None  -> Disables sending Pings (Prevents 20s disconnect)
    # ws_ping_timeout=None   -> Disables waiting for Pongs (Prevents timeouts)
    uvicorn.run(
        "api_server:app", 
        host="0.0.0.0",
        port=7861,
        loop="uvloop",
        ws_ping_interval=None, 
        ws_ping_timeout=None,
        log_level="info" #info, critical
    )
