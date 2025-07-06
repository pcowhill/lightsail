import asyncio
import websockets

connected_clients = set()

async def handler(websocket):
  connected_clients.add(websocket)
  try:
    async for message in websocket:
      print(f"Received: {message}")
      for client in connected_clients:
        if client != websocket:
          await client.send(f"{message}")
  finally:
    connected_clients.remove(websocket)

async def main():
  async with websockets.serve(handler, "localhost", 8765):
    print("✅ WebSocket server running on ws://localhost:8765")
    await asyncio.Future()

if __name__ == "__main__":
  asyncio.run(main())