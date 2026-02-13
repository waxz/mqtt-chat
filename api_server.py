#!/usr/bin/env python3
"""
api_server.py — High-Performance OpenAI-Compatible MQTT Proxy (Multi-Worker Edition)

Features:
- Full OpenAI Chat Completion API support.
- Multi-Worker Discovery: Lists every active browser tab as a unique model.
- Intelligent Routing: Routes requests to specific workers or load-balances across ready ones.
- Enhanced Session Isolation: Handles per-tab worker sessions (Zen v9.5).
"""

import json
import time
import uuid
import asyncio
import logging
import os
from typing import Optional, List, Dict, Any, Union, Literal, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi.responses import HTMLResponse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion
import uvicorn

# ============================================================
# CONFIGURATION
# ============================================================
class Config:
    BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "nxdev-org-mqtt-broker.hf.space")
    BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "443"))
    USE_TLS = os.getenv("MQTT_USE_TLS", "true").lower() in ("1", "true", "yes")
    WS_PATH = os.getenv("MQTT_WS_PATH", "/mqtt")
    
    API_HOST = "0.0.0.0"
    API_PORT = 8001
    
    TIMEOUT_SEC = 120.0
    SESSION_EXPIRY = 30.0  # Workers must heartbeat every 2s, 30s is generous

config = Config()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("openai-proxy")

# ============================================================
# MODELS
# ============================================================

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: float = 1.0

class ChoiceDelta(BaseModel):
    content: Optional[str] = None
    reasoning_content: Optional[str] = None

class ChoiceChunk(BaseModel):
    delta: ChoiceDelta
    finish_reason: Optional[str] = None
    index: int = 0

class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChoiceChunk]

# ============================================================
# MQTT BRIDGE ENGINE
# ============================================================

class OpenAIProxyEngine:
    def __init__(self):
        self.client_id = f"proxy-{uuid.uuid4().hex[:8]}"
        self.workers: Dict[str, Dict] = {} # sid -> {model, status, last_seen, host}
        self._queues: Dict[str, asyncio.Queue] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Paho MQTT Setup
        self.mqtt = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            transport="websockets"
        )
        if config.USE_TLS:
            self.mqtt.tls_set()
        self.mqtt.ws_set_options(path=config.WS_PATH, headers={"Sec-WebSocket-Protocol": "mqtt"})
        
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    def set_loop(self, loop):
        self._loop = loop

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            logger.info("✅ Proxy connected to MQTT broker")
            client.subscribe("arena-ai/+/response")
            client.subscribe("arena-ai/global/heartbeat")
            client.publish("arena-ai/global/discovery", "ping")
        else:
            logger.error(f"❌ MQTT Connection failed: {rc}")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())

            if topic == "arena-ai/global/heartbeat":
                sid = payload.get("id")
                if sid:
                    self.workers[sid] = {
                        "last_seen": time.time(),
                        "model": payload.get("model", "AI-Worker"),
                        "status": payload.get("status", "ready"),
                        "host": payload.get("host", "unknown")
                    }
                return

            if topic.endswith("/response"):
                rid = payload.get("id")
                if rid in self._queues and self._loop:
                    self._loop.call_soon_threadsafe(self._queues[rid].put_nowait, payload)
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")

    def get_active_workers(self):
        now = time.time()
        active = {}
        for sid, info in list(self.workers.items()):
            if now - info["last_seen"] < config.SESSION_EXPIRY:
                active[sid] = info
            else:
                del self.workers[sid]
        return active

    async def chat(self, req: ChatCompletionRequest) -> AsyncGenerator[Dict, None]:
        active = self.get_active_workers()
        target_sid = None

        # 1. Try exact SID match
        if req.model in active:
            target_sid = req.model
        # 2. Try "Model:SID" format match
        elif ":" in req.model:
            parts = req.model.split(":")
            potential_sid = parts[-1]
            if potential_sid in active:
                target_sid = potential_sid
        
        # 3. Fallback: Find worker by model name
        if not target_sid:
            candidates = [sid for sid, info in active.items() if info["model"] == req.model and info["status"] == "ready"]
            if candidates:
                target_sid = candidates[0]
        
        # 4. Final Fallback: First ready worker
        if not target_sid:
            ready = [sid for sid, info in active.items() if info["status"] == "ready"]
            if not ready:
                logger.error("❌ No active Zen workers available")
                raise HTTPException(status_code=503, detail="No active Zen Bridge workers found")
            target_sid = ready[0]

        req_id = f"req-{uuid.uuid4().hex[:12]}"
        q = asyncio.Queue()
        self._queues[req_id] = q
        
        mqtt_payload = {
            "id": req_id,
            "messages": [m.model_dump() for m in req.messages],
            "stream": req.stream,
            "temperature": req.temperature
        }

        logger.info(f"📤 [OpenAI] Start {req_id} -> Worker {target_sid} ({active[target_sid]['model']})")

        try:
            self.mqtt.publish(f"arena-ai/{target_sid}/request", json.dumps(mqtt_payload), qos=1)
            
            start = time.time()
            chunk_count = 0
            while True:
                if time.time() - start > config.TIMEOUT_SEC:
                    logger.warning(f"⏰ {req_id} timed out")
                    raise asyncio.TimeoutError()
                
                chunk = await q.get()
                chunk_count += 1
                yield chunk
                
                choices = chunk.get("choices", [])
                is_done = False
                if choices and choices[0].get("finish_reason"):
                    is_done = True
                elif chunk.get("object") == "chat.completion":
                    is_done = True
                
                if is_done:
                    while not q.empty():
                        extra = q.get_nowait()
                        yield extra
                        chunk_count += 1
                    logger.info(f"✅ [OpenAI] End {req_id} ({chunk_count} chunks)")
                    break
        finally:
            if req_id in self._queues:
                del self._queues[req_id]

# ============================================================
# API SERVER
# ============================================================

engine = OpenAIProxyEngine()

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.set_loop(asyncio.get_running_loop())
    engine.mqtt.connect(config.BROKER_HOST, config.BROKER_PORT)
    engine.mqtt.loop_start()
    yield
    engine.mqtt.loop_stop()
    engine.mqtt.disconnect()

app = FastAPI(title="Zen OpenAI Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def index():
    return "High-Performance OpenAI-Compatible MQTT Proxy (Multi-Worker Edition)"


@app.get("/v1/models")
async def models():
    active = engine.get_active_workers()
    data = []
    
    # Add a generic "auto" model
    data.append({"id": "auto", "object": "model", "owned_by": "zen-bridge"})
    
    for sid, info in active.items():
        # Represent each session as a model: "ModelName:SID"
        model_id = f"{info['model']}:{sid}"
        data.append({
            "id": model_id,
            "object": "model",
            "owned_by": "zen-bridge",
            "meta": {
                "sid": sid,
                "status": info["status"],
                "host": info["host"]
            }
        })
    return {"object": "list", "data": data}

@app.post("/v1/chat/completions")
async def chat(req: ChatCompletionRequest, request: Request):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if req.stream:
        async def stream_gen():
            try:
                async for chunk in engine.chat(req):
                    if await request.is_disconnected():
                        break
                    
                    choices = chunk.get("choices", [])
                    if not choices: continue
                    delta_data = choices[0].get("delta", {}) or choices[0].get("message", {})
                    
                    resp = ChatCompletionChunk(
                        id=chat_id, created=created, model=req.model,
                        choices=[ChoiceChunk(
                            delta=ChoiceDelta(
                                content=delta_data.get("content"),
                                reasoning_content=delta_data.get("reasoning_content")
                            ),
                            finish_reason=choices[0].get("finish_reason")
                        )]
                    )
                    yield f"data: {resp.model_dump_json(exclude_none=True)}\n\n"
                
                if not await request.is_disconnected():
                    yield "data: [DONE]\n\n"
            except Exception as e:
                logger.error(f"Stream Error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        
        return StreamingResponse(stream_gen(), media_type="text/event-stream")
    
    else:
        full_content = ""
        full_reasoning = ""
        async for chunk in engine.chat(req):
            choices = chunk.get("choices", [])
            if not choices: continue
            delta_data = choices[0].get("delta", {}) or choices[0].get("message", {})
            full_content += delta_data.get("content", "") or ""
            full_reasoning += delta_data.get("reasoning_content", "") or ""
        
        return {
            "id": chat_id, "object": "chat.completion", "created": created, "model": req.model,
            "choices": [{
                "message": {
                    "role": "assistant", 
                    "content": full_content, 
                    "reasoning_content": full_reasoning if full_reasoning else None
                },
                "finish_reason": "stop", "index": 0
            }]
        }

if __name__ == "__main__":
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
