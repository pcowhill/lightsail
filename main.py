from aiohttp import web
import asyncio

connected_clients = set()

# WebSocket handler
async def websocket_handler(request):
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  connected_clients.add(ws)
  print("Client connected.  Total: ", len(connected_clients))

  try:
    async for msg in ws:
      if msg.type == web.WSMsgType.TEXT:
        for client in connected_clients:
          if not client.closed and client != ws:
            await client.send_str(f"{msg.data}")
      elif msg.type == web.WSMsgType.ERROR:
        print(f"WebSocket error: {ws.exception()}")
  finally:
    connected_clients.remove(ws)
    print("Client disconnected.  Total: ", len(connected_clients))

  return ws

# HTTP handler
async def index(request):
  return web.FileResponse('index.html')

# App setup
app = web.Application()
app.add_routes([
  web.get('/', index),
  web.get('/ws', websocket_handler)
])

# Run both servers on the same port
if __name__ == "__main__":
  web.run_app(app, host='0.0.0.0', port=8080)