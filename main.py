import os
import asyncio
import json
from typing import Dict, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory ephemeral room registry (no persistence by design)
class RoomManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.room_locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, room_id: str) -> asyncio.Lock:
        if room_id not in self.room_locks:
            self.room_locks[room_id] = asyncio.Lock()
        return self.room_locks[room_id]

    async def connect(self, room_id: str, websocket: WebSocket):
        await websocket.accept()
        async with self._get_lock(room_id):
            if room_id not in self.rooms:
                self.rooms[room_id] = set()
            self.rooms[room_id].add(websocket)

    async def disconnect(self, room_id: str, websocket: WebSocket):
        async with self._get_lock(room_id):
            if room_id in self.rooms and websocket in self.rooms[room_id]:
                self.rooms[room_id].remove(websocket)
                # If last user leaves, destroy the room immediately (disposable masks)
                if len(self.rooms[room_id]) == 0:
                    del self.rooms[room_id]

    async def broadcast(self, room_id: str, message: dict, sender: WebSocket | None = None):
        async with self._get_lock(room_id):
            targets = list(self.rooms.get(room_id, set()))
        data = json.dumps(message)
        coros = []
        for ws in targets:
            if sender is not None and ws is sender:
                continue
            coros.append(ws.send_text(data))
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

manager = RoomManager()

@app.get("/")
def read_root():
    return {"message": "GPM Backend running", "ephemeral": True}

@app.get("/api/hello")
def hello():
    return {"message": "Hello from GPM backend"}

@app.get("/test")
def test_database():
    # This project intentionally does not use persistent storage for chats
    # Return simple health info
    return {
        "backend": "✅ Running",
        "ephemeral": True,
        "rooms_active": len(manager.rooms),
    }

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    # Query params can include mask=1 for disposable rooms (default behavior already disposes when empty)
    await manager.connect(room_id, websocket)
    # Notify others that a user joined (no identity data transmitted)
    await manager.broadcast(room_id, {"type": "presence", "event": "join"}, sender=websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # Pass-through as opaque ciphertext blob if not JSON
                payload = {"type": "cipher", "blob": raw}

            msg_type = payload.get("type")

            if msg_type == "dust":
                # Broadcast dust event to all peers in room; no server-side storage to wipe
                await manager.broadcast(room_id, {"type": "dust"})
                continue

            if msg_type == "typing":
                await manager.broadcast(room_id, {"type": "typing"}, sender=websocket)
                continue

            if msg_type == "cipher":
                # Relay E2EE ciphertext as-is; include metadata like ttl if provided
                envelope = {
                    "type": "cipher",
                    "ciphertext": payload.get("ciphertext") or payload.get("blob"),
                    "iv": payload.get("iv"),
                    "ttl": payload.get("ttl"),
                    "ts": payload.get("ts"),
                    "nonce": payload.get("nonce"),
                }
                await manager.broadcast(room_id, envelope, sender=websocket)
                continue

            # Unknown types are ignored to maintain minimal surface
        
    except WebSocketDisconnect:
        await manager.disconnect(room_id, websocket)
        await manager.broadcast(room_id, {"type": "presence", "event": "leave"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
