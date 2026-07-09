"""
CodeRoom backend — Flask + Flask-SocketIO
"""

import random
import string
import time

from flask import Flask, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "coderoom-dev-secret"
CORS(app, resources={r"/*": {"origins": "*"}})

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    transports=["polling"],
)

rooms = {}

DEFAULT_FILES = {
    "index.html": {
        "language": "html",
        "content": (
            "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\">\n"
            "  <title>Live Preview</title>\n  <link rel=\"stylesheet\" href=\"style.css\">\n"
            "</head>\n<body>\n  <h1>Hello, CodeRoom 👋</h1>\n"
            "  <p>Edit any file and watch this preview update in real time.</p>\n"
            "  <button id=\"btn\">Click me</button>\n  <script src=\"script.js\"></script>\n"
            "</body>\n</html>"
        ),
    },
    "style.css": {
        "language": "css",
        "content": (
            "body {\n  font-family: -apple-system, sans-serif;\n  background: #0f1115;\n"
            "  color: #eaeaea;\n  display: flex;\n  flex-direction: column;\n"
            "  align-items: center;\n  justify-content: center;\n  height: 100vh;\n"
            "  margin: 0;\n  text-align: center;\n}\n\nbutton {\n  background: #4fa8ff;\n"
            "  border: none;\n  color: #08131f;\n  padding: 10px 18px;\n"
            "  border-radius: 6px;\n  font-weight: 600;\n  cursor: pointer;\n}"
        ),
    },
    "script.js": {
        "language": "javascript",
        "content": (
            "document.getElementById('btn').addEventListener('click', () => {\n"
            "  alert('Hello from script.js!');\n});"
        ),
    },
}


def generate_room_id(length=6):
    alphabet = string.ascii_lowercase + string.digits
    while True:
        rid = "".join(random.choice(alphabet) for _ in range(length))
        if rid not in rooms:
            return rid


def get_or_create_room(room_id):
    if room_id not in rooms:
        rooms[room_id] = {
            "files": {k: dict(v) for k, v in DEFAULT_FILES.items()},
            "users": {},
        }
    return rooms[room_id]


def usernames_in_room(room_id):
    return list(rooms.get(room_id, {}).get("users", {}).values())


@app.route("/create-room", methods=["POST"])
def create_room():
    room_id = generate_room_id()
    get_or_create_room(room_id)
    return jsonify({"room": room_id})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "rooms": len(rooms)})


@socketio.on("connect")
def on_connect():
    print(f"[connect] {socketio_request_sid()}")


@socketio.on("disconnect")
def on_disconnect():
    sid = socketio_request_sid()
    for room_id, room in list(rooms.items()):
        if sid in room["users"]:
            username = room["users"].pop(sid)
            leave_room(room_id)
            emit(
                "user_left",
                {"username": username, "users": usernames_in_room(room_id)},
                to=room_id,
            )
            emit("room_users", usernames_in_room(room_id), to=room_id)
            print(f"[disconnect] {username} left {room_id}")
            if not room["users"]:
                rooms.pop(room_id, None)


@socketio.on("join")
def on_join(data):
    room_id = (data or {}).get("room", "").strip()
    username = (data or {}).get("username", "Anonymous").strip() or "Anonymous"
    if not room_id:
        emit("error", {"message": "Room ID is required"})
        return

    sid = socketio_request_sid()
    room = get_or_create_room(room_id)
    room["users"][sid] = username
    join_room(room_id)

    emit("load_code", {"files": room["files"]})

    emit(
        "user_joined",
        {"username": username, "users": usernames_in_room(room_id)},
        to=room_id,
        include_self=False,
    )
    emit("room_users", usernames_in_room(room_id), to=room_id)
    print(f"[join] {username} -> {room_id} ({len(room['users'])} online)")


@socketio.on("code_change")
def on_code_change(data):
    room_id = (data or {}).get("room", "").strip()
    code = (data or {}).get("code")
    if not room_id or room_id not in rooms or not isinstance(code, dict):
        return
    rooms[room_id]["files"] = code
    emit("update_code", {"files": code}, to=room_id, include_self=False)


@socketio.on("typing")
def on_typing(data):
    room_id = (data or {}).get("room", "").strip()
    username = (data or {}).get("username", "")
    if not room_id:
        return
    emit("typing", {"username": username}, to=room_id, include_self=False)


@socketio.on("stop_typing")
def on_stop_typing(data):
    room_id = (data or {}).get("room", "").strip()
    username = (data or {}).get("username", "")
    if not room_id:
        return
    emit("stop_typing", {"username": username}, to=room_id, include_self=False)


@socketio.on("chat_message")
def on_chat_message(data):
    room_id = (data or {}).get("room", "").strip()
    if not room_id:
        return
    payload = {
        "username": (data or {}).get("username", "Anonymous"),
        "message": (data or {}).get("message", ""),
        "ts": (data or {}).get("ts", int(time.time() * 1000)),
    }
    emit("chat_message", payload, to=room_id, include_self=False)


def socketio_request_sid():
    from flask import request
    return request.sid


if __name__ == "__main__":
    print("Starting CodeRoom backend on 0.0.0.0:8000")
    print("Point the frontend's SERVER field to: http://<your-termux-ip>:8000")
    socketio.run(app, host="0.0.0.0", port=8000, debug=False, allow_unsafe_werkzeug=True)
