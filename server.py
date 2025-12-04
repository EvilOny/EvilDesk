import asyncio
import json
import base64
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
from winrt.windows.storage.streams import DataReader
import websockets

clients = set()
last_state = None

async def get_player_state():
    sessions = await MediaManager.request_async()
    current = sessions.get_current_session()
    if not current:
        return None

    info = await current.try_get_media_properties_async()

    state = current.get_playback_info()
    timeline = current.get_timeline_properties()

    player_state = {
        "track": info.title,
        "artist": info.artist,
        "is_playing": state.playback_status == 4,
        "position": int(timeline.position.total_seconds()),
        "duration": int(timeline.end_time.total_seconds()),
        "cover": None
    }

    # --- Обложка ---
    thumb = info.thumbnail
    if thumb:
        stream = thumb.open_read_async().get()
        reader = DataReader(stream)
        reader.load_async(int(stream.size)).get()
        ibuffer = reader.detach_buffer()
        buffer = bytes(memoryview(ibuffer))
        player_state["cover"] = base64.b64encode(buffer).decode("utf-8")
    else:
        player_state["cover"] = None

    return player_state


async def broadcast_state():
    global last_state

    while True:
        try:
            state = await get_player_state()
            if state and state != last_state:
                last_state = state
                message = json.dumps({"type": "state", "data": state})
                for ws in clients.copy():
                    await ws.send(message)
                print("🎧 Обновление:", state["track"])
        except Exception as e:
            print("Ошибка обновления:", e)

        await asyncio.sleep(1)


async def handler(ws):
    clients.add(ws)
    print("✅ Клиент подключен")

    try:
        if last_state:
            await ws.send(json.dumps({"type": "state", "data": last_state}))

        async for msg in ws:
            data = json.loads(msg)
            cmd = data.get("cmd")

            sessions = await MediaManager.request_async()
            current = sessions.get_current_session()
            if not current:
                continue

            if cmd == "playpause":
                await current.try_toggle_play_pause_async()
            elif cmd == "next":
                await current.try_skip_next_async()
            elif cmd == "prev":
                await current.try_skip_previous_async()

    finally:
        clients.remove(ws)
        print("❌ Клиент отключён")


async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        print("✅ WebSocket сервер запущен")
        await broadcast_state()

asyncio.run(main())
