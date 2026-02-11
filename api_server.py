#!/usr/bin/env python3
"""
api_server.py — Enterprise-grade OpenAI-Compatible MQTT Bridge

Features:
- Strict OpenAI API Spec compliance (Pydantic v2).
- Proactive Worker Discovery (Broadcast Ping).
- Robust Asyncio/Thread bridging.
- Graceful handling of client disconnects and timeouts.
"""

import json
import time
import uuid
import asyncio
import argparse
import logging
import os
import threading
from typing import Optional, List, Dict, Any, Union, Literal, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion
import uvicorn

# ============================================================
# CONFIGURATION
# ============================================================
class Config:
    # BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "nxdev-org-mqtt-broker.hf.space")
    # BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "443"))
    # USE_TLS = os.getenv("MQTT_USE_TLS", "true").lower() in ("1", "true", "yes")

    BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
    BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "7860"))
    USE_TLS = os.getenv("MQTT_USE_TLS", "false").lower() in ("1", "true", "yes")
    
    WS_PATH = os.getenv("MQTT_WS_PATH", "/mqtt")
    
    API_HOST = os.getenv("API_HOST", "0.0.0.0")
    API_PORT = int(os.getenv("API_PORT", "8001"))
    
    TIMEOUT_SEC = 60.0        # Max time to wait for first token
    SESSION_EXPIRY = 120.0    # Seconds before a model is considered offline

config = Config()
logger = logging.getLogger("arena-api")

# ============================================================
# OPENAI PYDANTIC MODELS (Strict Spec)
# ============================================================

class ChatMessage(BaseModel):
    role: str
    content: str
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: List[ChatMessage]
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None

# -- Responses --

class ChoiceDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None  # DeepSeek/Thinking extension

class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = ""
    reasoning_content: Optional[str] = None

class Choice(BaseModel):
    index: int
    message: ChoiceMessage
    finish_reason: Optional[str] = None

class ChoiceChunk(BaseModel):
    index: int
    delta: ChoiceDelta
    finish_reason: Optional[str] = None

class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    system_fingerprint: Optional[str] = "fp_mqtt_bridge"
    choices: List[Choice]
    usage: UsageInfo

class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    system_fingerprint: Optional[str] = "fp_mqtt_bridge"
    choices: List[ChoiceChunk]

# ============================================================
# MQTT BRIDGE CORE
# ============================================================

class MQTTBridge:
    def __init__(self):
        self.client_id = f"api-srv-{uuid.uuid4().hex[:8]}"
        self.sessions: Dict[str, Dict] = {}  # { session_id: { last_seen, host, status } }
        
        # Request routing: req_id -> asyncio.Queue
        self._response_queues: Dict[str, asyncio.Queue] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Paho Client
        self.client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self.client_id,
            transport="websockets",
            protocol=mqtt.MQTTv311,
        )
        
        if config.USE_TLS:
            self.client.tls_set()
            
        self.client.ws_set_options(path=config.WS_PATH, headers={"Sec-WebSocket-Protocol": "mqtt"})
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def set_loop(self, loop):
        self._loop = loop

    def connect(self):
        logger.info(f"🔌 Connecting to Broker: {config.BROKER_HOST}:{config.BROKER_PORT}")
        try:
            self.client.connect(config.BROKER_HOST, config.BROKER_PORT, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            logger.error(f"❌ Connection failed: {e}")
            raise e

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    # --- MQTT Callbacks (Threaded) ---

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            logger.info("✅ MQTT Connected. Subscribing to heartbeats...")
            client.subscribe("arena-ai/+/response")        # Listen for all worker responses
            client.subscribe("arena-ai/global/heartbeat")  # Listen for worker presence
            
            # PROACTIVE DISCOVERY: Tell all workers to announce themselves immediately
            self.client.publish("arena-ai/global/discovery", "ping", retain=False)
        else:
            logger.error(f"❌ MQTT Connect Failed. RC={rc}")

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        logger.warning(f"⚠️ MQTT Disconnected (RC={rc})")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())

            # 1. Heartbeat Handling
            if topic == "arena-ai/global/heartbeat":
                print(f"⚠️ Get heartbeat payload:{payload}")
                sid = payload.get("id")
                if sid:
                    status = {
                        "last_seen": time.time(),
                        "host": payload.get("host", "unknown"),
                        "status": payload.get("status", "ready"),
                        "model": payload.get("model", sid)
                    }
                    print(f"⚠️ Update sessions sid:{sid}, status:{status}")
                    self.sessions[sid] = status
                return

            # 2. Response Handling
            # Topic format: arena-ai/{session_id}/response
            if topic.endswith("/response"):
                req_id = payload.get("id")
                if req_id and req_id in self._response_queues:
                    # Thread-safe put into asyncio queue
                    if self._loop:
                        self._loop.call_soon_threadsafe(
                            self._response_queues[req_id].put_nowait, payload
                        )
                        
        except Exception as e:
            logger.error(f"Message processing error: {e}")

    # --- Async API Methods ---

    def get_active_models(self) -> List[Dict]:
        """Return list of models seen in the last SESSION_EXPIRY seconds."""
        now = time.time()
        active = []
        # Filter stale sessions
        stale_ids = []
        for sid, info in self.sessions.items():
            if now - info["last_seen"] > config.SESSION_EXPIRY:
                stale_ids.append(sid)
            else:
                active.append({
                    "id": sid,
                    "object": "model",
                    "created": int(info["last_seen"]),
                    "owned_by": info["host"]
                })
        
        # Cleanup stale
        for sid in stale_ids:
            del self.sessions[sid]
            
        return active

    async def send_chat_request(self, req: ChatCompletionRequest) -> AsyncGenerator[Dict, None]:
        """
        Orchestrates the request:
        1. Selects Model
        2. Publishes MQTT Request
        3. Listens for MQTT Responses
        4. Yields chunks (or full response)
        """
        # 1. Resolve Model
        target_session = self._resolve_session(req.model)
        
        # 2. Prepare Request
        req_id = uuid.uuid4().hex
        response_queue = asyncio.Queue()
        self._response_queues[req_id] = response_queue

        mqtt_payload = {
            "id": req_id,
            "messages": [m.model_dump() for m in req.messages],
            "stream": req.stream,
            "temperature": req.temperature,
            # Pass other params if needed
        }
        
        topic = f"arena-ai/{target_session}/request"
        logger.info(f"📤 Routing request {req_id[:6]} -> {target_session}")

        try:
            # Publish (Non-blocking)
            self.client.publish(topic, json.dumps(mqtt_payload), qos=1)
            
            # 3. Wait for Responses
            start_time = time.time()
            first_packet = True
            
            while True:
                # Calculate Timeout
                elapsed = time.time() - start_time
                remaining = config.TIMEOUT_SEC - elapsed
                
                if remaining <= 0:
                    raise HTTPException(status_code=504, detail="Worker timed out waiting for response")

                try:
                    # Wait for next chunk
                    raw_msg = await asyncio.wait_for(response_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                     raise HTTPException(status_code=504, detail="Worker timed out")

                # Check for worker-side errors
                if "error" in raw_msg:
                    raise HTTPException(status_code=502, detail=f"Worker Error: {raw_msg['error']}")

                # Yield logic
                yield raw_msg
                
                # Check for done
                choices = raw_msg.get("choices", [])
                if choices and choices[0].get("finish_reason"):
                    break
                    
                # Standardize object type check
                if raw_msg.get("object") == "chat.completion":
                    break

        finally:
            # Cleanup
            if req_id in self._response_queues:
                del self._response_queues[req_id]

    def _resolve_session(self, model_name: str) -> str:
        # 1. Exact Match (Session ID)
        if model_name in self.sessions:
            return model_name
            
        # 2. "auto" or empty -> Pick any active
        active = self.get_active_models()
        if not active:
             raise HTTPException(status_code=503, detail="No active workers found. Please open the UserScript.")
             
        if model_name in ["auto", "default", "gpt-3.5-turbo"]:
            return active[0]["id"]
            
        # 3. Search by partial name (if worker sends friendly model name)
        # (Simplified: just assuming model_name == session_id for now)
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found.")

# ============================================================
# FASTAPI APP
# ============================================================

bridge = MQTTBridge()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print(f"🚀 Starting API Server on {config.API_HOST}:{config.API_PORT}")
    print(f"🔗 Bridging to MQTT: {config.BROKER_HOST}")
    
    bridge.set_loop(asyncio.get_running_loop())
    bridge.connect()
    logger.info("🚀 API Server Started")
    yield
    # Shutdown
    bridge.disconnect()
    logger.info("👋 API Server Stopped")

app = FastAPI(title="MQTT OpenAI Bridge", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.get("/")
async def root():
    return {"message": "AI MQTT Bridge" }

# --- Exception Handler for OpenAI format ---
@app.exception_handler(HTTPException)
async def openai_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.detail,
                "type": "api_error",
                "param": None,
                "code": exc.status_code
            }
        }
    )

# ============================================================
# ENDPOINTS
# ============================================================

@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": bridge.get_active_models()}

@app.get("/health")
async def health():
    return {
        "status": "ok", 
        "mqtt": bridge.client.is_connected(),
        "workers": len(bridge.sessions)
    }

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    
    # Generate authoritative ID for this interaction
    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    created_ts = int(time.time())

    # --- Streaming Response ---
    if req.stream:
        async def generator():
            try:
                async for raw_chunk in bridge.send_chat_request(req):
                    if await request.is_disconnected():
                        logger.info("Client disconnected, stopping stream")
                        break
                    
                    # Convert raw worker JSON to Pydantic strict format
                    # Worker sends: { choices: [{ delta: { content: "x" } }] }
                    # We need to ensure fields exist
                    
                    worker_choices = raw_chunk.get("choices", [])
                    delta = {}
                    finish_reason = None
                    
                    if worker_choices:
                        delta = worker_choices[0].get("delta", {}) or worker_choices[0].get("message", {})
                        finish_reason = worker_choices[0].get("finish_reason")

                    chunk_resp = ChatCompletionChunk(
                        id=chat_id,
                        created=created_ts,
                        model=req.model,
                        choices=[
                            ChoiceChunk(
                                index=0,
                                delta=ChoiceDelta(
                                    content=delta.get("content"),
                                    reasoning_content=delta.get("reasoning_content"),
                                    role=delta.get("role") if delta.get("role") else None
                                ),
                                finish_reason=finish_reason
                            )
                        ]
                    )
                    
                    yield f"data: {chunk_resp.model_dump_json(exclude_none=True)}\n\n"
                    
                    if finish_reason:
                        yield "data: [DONE]\n\n"
                        break
                        
            except HTTPException as e:
                # If streaming started, we can't easily change status code, 
                # but we can send an error object in the stream
                err_payload = json.dumps({"error": {"message": e.detail, "code": e.status_code}})
                yield f"data: {err_payload}\n\n"
            except Exception as e:
                logger.error(f"Stream error: {e}")
                
        return StreamingResponse(generator(), media_type="text/event-stream")

    # --- Non-Streaming Response (Buffered) ---
    else:
        full_content = ""
        full_reasoning = ""
        finish_reason = "stop"
        
        async for raw_chunk in bridge.send_chat_request(req):
            if await request.is_disconnected():
                raise HTTPException(499, "Client Closed Request")

            # Handle both "stream-like" chunks and "full" responses from worker
            choices = raw_chunk.get("choices", [])
            if not choices: continue
            
            delta = choices[0].get("delta", {}) or choices[0].get("message", {})
            
            if "content" in delta and delta["content"]:
                full_content += delta["content"]
            if "reasoning_content" in delta and delta["reasoning_content"]:
                full_reasoning += delta["reasoning_content"]
                
            if choices[0].get("finish_reason"):
                finish_reason = choices[0].get("finish_reason")

        return ChatCompletionResponse(
            id=chat_id,
            created=created_ts,
            model=req.model,
            choices=[
                Choice(
                    index=0,
                    message=ChoiceMessage(
                        role="assistant",
                        content=full_content,
                        reasoning_content=full_reasoning if full_reasoning else None
                    ),
                    finish_reason=finish_reason
                )
            ],
            usage=UsageInfo(
                completion_tokens=len(full_content) // 4, # Rough estimate
                prompt_tokens=len(str(req.messages)) // 4,
                total_tokens=0
            )
        )

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    print(f"🚀 Starting API Server on {config.API_HOST}:{config.API_PORT}")
    print(f"🔗 Bridging to MQTT: {config.BROKER_HOST}")
    
    uvicorn.run(
        app, 
        host=config.API_HOST, 
        port=config.API_PORT,
        log_level="info"
    )