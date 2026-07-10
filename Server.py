"""
CodeRoom backend — Flask + Flask-SocketIO + PRoot Sandbox
"""

import os
import pty
import shutil
import random
import select
import string
import signal
import subprocess
import threading
import time
from flask import Flask, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
CORS(app, resources={r"/*": {"origins": "*"}})

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    transports=["polling"],
)

BASE_ROOTFS = "/var/coderoom/base"
ROOMS_DIR = "/var/coderoom/rooms"
WORKSPACES_DIR = "/var/coderoom/workspaces"

rooms = {}
terminal_sessions = {}

DEFAULT_FILES = {
    "index.html": {
        "language": "html",
        "content": (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="UTF-8">\n'
            '  <title>Live Preview</title>\n  <link rel="stylesheet" href="style.css">\n'
            '</head>\n<body>\n  <h1>Hello, CodeRoom 👋</h1>\n'
            '  <p>Edit files and run them in the terminal below.</p>\n'
            '  <button id="btn">Click me</button>\n  <script src="script.js"></script>\n'
            '</body>\n</html>'
        ),
    },
    "style.css": {
        "language": "css",
        "content": (
            "body {\n  font-family: -apple-system, sans-serif;\n  background: #0f1115;\n"
            "  color: #eaeaea;\n  margin: 40px;\n  text-align: center;\n}\n"
            "button {\n  background: #4fa8ff;\n  border: none;\n  color: #08131f;\n"
            "  padding: 10px 18px;\n  border-radius: 6px;\n  font-weight: 600;\n  cursor: pointer;\n}"
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


def init_room_fs(room_id):
    room_root = os.path.join(ROOMS_DIR, room_id)
    workspace = os.path.join(WORKSPACES_DIR, room_id)
    os.makedirs(room_root, exist_ok=True)
    os.makedirs(workspace, exist_ok=True)
    for fname, fdata in DEFAULT_FILES.items():
        fpath = os.path.join(workspace, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w") as f:
                f.write(fdata["content"])
    return room_root, workspace


def cleanup_room_fs(room_id):
    for d in [os.path.join(ROOMS_DIR, room_id), os.path.join(WORKSPACES_DIR, room_id)]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)


def get_or_create_room(room_id):
    if room_id not in rooms:
        room_root, workspace = init_room_fs(room_id)
        rooms[room_id] = {
            "rootfs": room_root,
            "workspace": workspace,
            "files": {k: dict(v) for k, v in DEFAULT_FILES.items()},
            "users": {},
        }
    return rooms[room_id]


def usernames_in_room(room_id):
    return list(rooms.get(room_id, {}).get("users", {}).values())


def read_workspace_files(room_id):
    room = rooms.get(room_id)
    if not room:
        return {}
    workspace = room["workspace"]
    files = {}
    for fname in os.listdir(workspace):
        fpath = os.path.join(workspace, fname)
        if os.path.isfile(fpath):
            try:
                with open(fpath) as f:
                    ext = fname.split(".")[-1] if "." in fname else ""
                    files[fname] = {
                        "language": ext,
                        "content": f.read(),
                    }
            except Exception:
                pass
    return files


def write_workspace_files(room_id, files):
    room = rooms.get(room_id)
    if not room:
        return
    workspace = room["workspace"]
    for fname, fdata in files.items():
        content = fdata.get("content", "") if isinstance(fdata, dict) else fdata
        fpath = os.path.join(workspace, fname)
        os.makedirs(os.path.dirname(fpath) or workspace, exist_ok=True)
        with open(fpath, "w") as f:
            f.write(content)


class TerminalSession:
    def __init__(self, sid, room_id, socketio_instance):
        self.sid = sid
        self.room_id = room_id
        self.socketio = socketio_instance
        self.master_fd = None
        self.slave_fd = None
        self.process = None
        self.running = False
        self.thread = None

    def start(self):
        room = rooms.get(self.room_id)
        if not room:
            return
        workspace = room["workspace"]
        self.master_fd, self.slave_fd = pty.openpty()
        self.process = subprocess.Popen(
            [
                "proot",
                "-S", BASE_ROOTFS,
                "-b", f"{workspace}:/workspace",
                "-w", "/workspace",
                "/bin/bash",
                "--login",
            ],
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            close_fds=True,
            preexec_fn=lambda: signal.signal(signal.SIGCHLD, signal.SIG_DFL),
        )
        os.close(self.slave_fd)
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def _read_loop(self):
        while self.running:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.1)
                if r:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        self.socketio.emit("terminal_output", {"data": data.decode("utf-8", errors="replace")}, to=self.sid)
                    else:
                        break
            except (OSError, ValueError):
                break
        self.stop()

    def write(self, data):
        if self.running and self.master_fd:
            try:
                os.write(self.master_fd, data.encode("utf-8") if isinstance(data, str) else data)
            except OSError:
                self.stop()

    def resize(self, cols, rows):
        if self.master_fd:
            try:
                import fcntl, termios, struct
                packed = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
            except Exception:
                pass

    def stop(self):
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        if self.master_fd:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


@app.route("/create-room", methods=["POST"])
def create_room():
    room_id = generate_room_id()
    get_or_create_room(room_id)
    return jsonify({"room": room_id})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "rooms": len(rooms)})


@socketio.on("connect")
def on_connect():
    pass


@socketio.on("disconnect")
def on_disconnect():
    sid = request_sid()
    if sid in terminal_sessions:
        terminal_sessions[sid].stop()
        del terminal_sessions[sid]
    for room_id, room in list(rooms.items()):
        if sid in room["users"]:
            username = room["users"].pop(sid)
            leave_room(room_id)
            emit("user_left", {"username": username, "users": usernames_in_room(room_id)}, to=room_id)
            emit("room_users", usernames_in_room(room_id), to=room_id)
            if not room["users"]:
                cleanup_room_fs(room_id)
                rooms.pop(room_id, None)


@socketio.on("join")
def on_join(data):
    room_id = (data or {}).get("room", "").strip()
    username = (data or {}).get("username", "Anonymous").strip() or "Anonymous"
    if not room_id:
        emit("error", {"message": "Room ID is required"})
        return

    sid = request_sid()
    room = get_or_create_room(room_id)
    room["users"][sid] = username
    join_room(room_id)

    emit("load_code", {"files": read_workspace_files(room_id)})

    emit("user_joined", {"username": username, "users": usernames_in_room(room_id)}, to=room_id, include_self=False)
    emit("room_users", usernames_in_room(room_id), to=room_id)

    ts = TerminalSession(sid, room_id, socketio)
    terminal_sessions[sid] = ts
    ts.start()


@socketio.on("code_change")
def on_code_change(data):
    room_id = (data or {}).get("room", "").strip()
    code = (data or {}).get("code")
    if not room_id or room_id not in rooms or not isinstance(code, dict):
        return
    rooms[room_id]["files"] = code
    write_workspace_files(room_id, code)
    emit("update_code", {"files": code}, to=room_id, include_self=False)


@socketio.on("terminal_input")
def on_terminal_input(data):
    sid = request_sid()
    if sid in terminal_sessions:
        terminal_sessions[sid].write((data or {}).get("data", ""))


@socketio.on("terminal_resize")
def on_terminal_resize(data):
    sid = request_sid()
    if sid in terminal_sessions:
        d = data or {}
        terminal_sessions[sid].resize(d.get("cols", 80), d.get("rows", 24))


@socketio.on("chat_message")
def on_chat_message(data):
    room_id = (data or {}).get("room", "").strip()
    if not room_id:
        return
    emit("chat_message", {
        "username": (data or {}).get("username", "Anonymous"),
        "message": (data or {}).get("message", ""),
        "ts": (data or {}).get("ts", int(time.time() * 1000)),
    }, to=room_id, include_self=False)


def request_sid():
    from flask import request
    return request.sid


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting CodeRoom on 0.0.0.0:{port}")
    print("Rooms get isolated PRoot sandboxes.")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
