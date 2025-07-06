from aiohttp import web
import os

chat_connected_clients = set()

# Chat WebSocket handler
async def chat_websocket_handler(request):
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  chat_connected_clients.add(ws)
  print("Client connected.  Total: ", len(chat_connected_clients))

  try:
    async for msg in ws:
      if msg.type == web.WSMsgType.TEXT:
        for client in chat_connected_clients:
          if not client.closed and client != ws:
            await client.send_str(f"{msg.data}")
      elif msg.type == web.WSMsgType.ERROR:
        print(f"WebSocket error: {ws.exception()}")
  finally:
    chat_connected_clients.remove(ws)
    print("Client disconnected.  Total: ", len(chat_connected_clients))

  return ws

draw_connected_clients = set()

# Chat WebSocket handler
async def draw_websocket_handler(request):
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  draw_connected_clients.add(ws)
  print("Client connected.  Total: ", len(draw_connected_clients))

  try:
    async for msg in ws:
      if msg.type == web.WSMsgType.TEXT:
        for client in draw_connected_clients:
          if not client.closed and client != ws:
            await client.send_str(f"{msg.data}")
      elif msg.type == web.WSMsgType.ERROR:
        print(f"WebSocket error: {ws.exception()}")
  finally:
    draw_connected_clients.remove(ws)
    print("Client disconnected.  Total: ", len(draw_connected_clients))

  return ws

# HTTP handler
async def index(request):
  return web.FileResponse('index.html')

# App setup
app = web.Application()
app.add_routes([
  web.get('/', index),
  web.get('/ws/chat', chat_websocket_handler),
  web.get('/ws/draw', draw_websocket_handler)
])
app.router.add_static('/', path=os.path.abspath('.'), show_index=True)

# Run both servers on the same port
if __name__ == "__main__":
  web.run_app(app, host='0.0.0.0', port=8080)