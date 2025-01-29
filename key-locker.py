#!/usr/bin/env python3
import getpass
import os
import pathlib
import string
import subprocess
import time
import json
import sys
import tomllib

arg_cmd = sys.argv[1]
commands: dict = {}

def valid_name(name: str) -> bool:
    return not set(name).difference(string.ascii_letters + string.digits + "_-")


class ValidNameError(Exception): pass


def get_config() -> dict:
    with open(os.path.expanduser("~/.config/key-locker.toml"), "rb") as f:
        data: dict = tomllib.load(f)
    if not valid_name(data["name"]):
        print("Name not valid", file=sys.stderr)
        exit(1)
    return data


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
    os.mkfifo(fifo_path, 0o640)

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


def root_fail(fifo_path: str, code: int, msg: str):
    with open(fifo_path, "w") as fifo:
        json.dump({
            "code": code,
            "stderr": msg
        }, fifo)
        fifo.flush()


def root_open(fifo_path: str, passwd: str, name: str, image: str, mount: str):
    if not valid_name(name):
        raise ValidNameError("Not not valid")
    subprocess.run([
        "cryptsetup", "open", "--type", "luks", image, f"key-locker-{name}"
    ], check=True, capture_output=True, input=passwd.encode('utf-8'))
    subprocess.run([
        "mount", "-t", "ext4", f"/dev/mapper/key-locker-{name}", mount
    ], check=True, capture_output=True)
    root_success(fifo_path)


def root_close(fifo_path: str, name: str, mount: str):
    if not valid_name(name):
        raise ValidNameError("Not not valid")
    subprocess.run([
        "umount", mount
    ], check=True, capture_output=True)
    subprocess.run([
        "cryptsetup", "close", f"key-locker-{name}"
    ], check=True, capture_output=True)
    root_success(fifo_path)


def process_queue(recv_fifo_path: str):
    if not os.path.exists(recv_fifo_path):
        time.sleep(1)
        return
    path_gid = os.stat(recv_fifo_path).st_gid
    with open(recv_fifo_path, "r") as fifo:
        data = json.load(fifo)
    fifo_path = data["fifo"]
    os.mkfifo(fifo_path, 0o640)
    os.chown(fifo_path, 0, path_gid)
    try:
        match data:
            case {"cmd": "open"}:
                root_open(fifo_path, data["passwd"], data["name"], data["image"], data["mount"])
            case {"cmd": "close"}:
                root_close(fifo_path, data["name"], data["mount"])
    except subprocess.CalledProcessError as e:
        root_fail(fifo_path, e.returncode, str(e.stderr))
    except ValidNameError:
        root_fail(fifo_path, 1, "Name not valid")
    except KeyError as e:
        root_fail(fifo_path, 1, e.__str__())
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