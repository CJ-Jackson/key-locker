#!/usr/bin/env python3
import getpass
import os
import pathlib
import subprocess
import time
import json
import sys
import tomllib

arg_cmd = sys.argv[1]
commands: dict = {}


def get_config() -> dict:
    with open(os.path.expanduser("~/.config/key-locker.toml"), "rb") as f:
        return tomllib.load(f)


def handle_recv_fifo(fifo_path: str):
    while not os.path.exists(fifo_path):
        time.sleep(1)
    with open(fifo_path, "r") as fifo:
        data = json.load(fifo)
    if data.get("stderr", None):
        print(data["stderr"], file=sys.stderr)
    if data.get("stdout", None):
        print(data["stdout"])
    exit(data.get("code", 0))


def create_send_fifi_add_to_queue() -> str:
    fifo_path = f"/tmp/key-locker-recv-fifo-{time.time()}"
    os.mkfifo(fifo_path)

    pathlib.Path(f"/tmp/key-locker-queue/key-locker-{time.time()}-queue").write_text(fifo_path, "utf-8")

    # Trigger systemd oneshot
    pathlib.Path("/tmp/key-locker.path").touch()

    return fifo_path


def user_open():
    fifo_recv_path = f"/tmp/key-locker-user-open-fifo-{time.time()}"

    # Get password from QR-Code
    process = subprocess.run(["zbarcam", "--raw", "-1"], check=True, capture_output=True)

    data = {
        "cmd": "open",
        "fifo": fifo_recv_path,
        "passwd": process.stdout.decode('utf-8').strip()
    } | get_config()

    fifo_send_path = create_send_fifi_add_to_queue()

    with open(fifo_send_path, "w") as fifo:
        json.dump(data, fifo)
        fifo.flush()

    os.remove(fifo_send_path)

    handle_recv_fifo(fifo_recv_path)


commands["open"] = user_open


def user_close():
    fifo_recv_path = f"/tmp/key-locker-user-close-fifo-{time.time()}"

    data = {
        "cmd": "close",
        "fifo": fifo_recv_path,
    } | get_config()

    fifo_send_path = create_send_fifi_add_to_queue()

    with open(fifo_send_path, "w") as fifo:
        json.dump(data, fifo)
        fifo.flush()

    os.remove(fifo_send_path)

    handle_recv_fifo(fifo_recv_path)


commands["close"] = user_close


def root_success(fifo_path: str):
    with open(fifo_path, "w") as fifo:
        json.dump({
            "code": 0,
            "stdout": "success"
        }, fifo)
        fifo.flush()


def root_open(fifo_path: str, passwd: str, name: str, image: str, mount: str):
    try:
        subprocess.run([
            "cryptsetup", "open", "--type", "luks", image, f"key-locker-{name}"
        ], check=True, capture_output=True, input=passwd.encode('utf-8'))
        subprocess.run([
            "mount", "-t", "ext4", f"/dev/mapper/key-locker-{name}", mount
        ], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        with open(fifo_path, "w") as fifo:
            json.dump({
                "code": e.returncode,
                "stderr": str(e.stderr)
            }, fifo)
            fifo.flush()
            return
    root_success(fifo_path)


def root_close(fifo_path: str, name: str, mount: str):
    try:
        subprocess.run([
            "umount", mount
        ], check=True, capture_output=True)
        subprocess.run([
            "cryptsetup", "close", f"key-locker-{name}"
        ])
    except subprocess.CalledProcessError as e:
        with open(fifo_path, "w") as fifo:
            json.dump({
                "code": e.returncode,
                "stderr": str(e.stderr)
            }, fifo)
            fifo.flush()
    root_success(fifo_path)


def process_queue(recv_fifo_path: str):
    if not os.path.exists(recv_fifo_path):
        time.sleep(1)
        return
    with open(recv_fifo_path, "r") as fifo:
        data = json.load(fifo)
    fifo_path = data["fifo"]
    os.mkfifo(fifo_path)
    match data:
        case {"cmd": "open"}:
            root_open(fifo_path, data["passwd"], data["name"], data["image"], data["mount"])
        case {"cmd": "close"}:
            root_close(fifo_path, data["name"], data["mount"])
    os.remove(fifo_path)
    time.sleep(1)


def recv():
    if getpass.getuser() != "root":
        print("Must be root to run `recv`", file=sys.stderr)
        exit(1)
    for queue in pathlib.Path("/tmp/key-locker-queue").glob("key-locker-*-queue"):
        fifo_path = pathlib.Path(str(queue)).read_text('utf-8').strip()
        os.remove(str(queue))
        process_queue(fifo_path)


commands["recv"] = recv

try:
    commands[arg_cmd]()
except KeyError:
    print("Could not find command", file=sys.stderr)