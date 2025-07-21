from aiohttp import web
import os
import json
import random

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

game_connected_clients = set()

worms = [[random.randint(-1000, 1000) for i in range(2)] for j in range(10)]

# Chat WebSocket handler
async def game_websocket_handler(request):
  ws = web.WebSocketResponse()
  await ws.prepare(request)

  game_connected_clients.add(ws)
  ws_id = random.getrandbits(32)
  print("Client connected.  Total: ", len(game_connected_clients))

  worm_data = {
    "type": "worms",
    "positions": worms
  }
  await ws.send_str(json.dumps(worm_data))

  try:
    async for msg in ws:
      if msg.type == web.WSMsgType.TEXT:
        try:
          data = json.loads(msg.data)
        except json.JSONDecodeError as e:
          print(f"JSON decode error: {e}")
          continue
        if data["type"] in ["movement", "connect"]:
          data["id"] = ws_id
          for client in game_connected_clients:
            if not client.closed and client != ws:
                await client.send_str(json.dumps(data))
        elif data["type"] == "eat":
          worm_data["positions"][data["worm_id"]] = [random.randint(-1000, 1000) for i in range(2)]
          for client in game_connected_clients:
            if not client.closed:
              await ws.send_str(json.dumps(worm_data))
      elif msg.type == web.WSMsgType.ERROR:
        print(f"WebSocket error: {ws.exception()}")
  finally:
    game_connected_clients.remove(ws)
    print("Client disconnected.  Total: ", len(game_connected_clients))
    for client in game_connected_clients:
      if not client.closed:
        data = {
          "type": "disconnect",
          "id": ws_id
        }
        await client.send_str(json.dumps(data))

  return ws

# HTTP handler
async def index(request):
  return web.FileResponse('index.html')

# App setup
app = web.Application()
app.add_routes([
  web.get('/', index),
  web.get('/ws/chat', chat_websocket_handler),
  web.get('/ws/draw', draw_websocket_handler),
  web.get('/ws/game', game_websocket_handler),
])
app.router.add_static('/', path=os.path.abspath('.'), show_index=True)

# Run both servers on the same port
if __name__ == "__main__":
  web.run_app(app, host='0.0.0.0', port=8080)