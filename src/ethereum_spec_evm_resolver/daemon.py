import json
import os
import socketserver
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import signal
from socket import socket
from threading import Thread
from time import sleep
from typing import Any, Optional, Tuple, Union, List

from platformdirs import user_runtime_dir
from requests_unixsocket import Session

from .forks import get_fork_resolution

runtime_dir = Path(user_runtime_dir("ethereum-spec-evm-resolver"))


class _EvmToolHandler(BaseHTTPRequestHandler):
    def log_request(self, *args):
        """Don't log requests"""
        pass

    def do_POST(self) -> None:
        content_length = int(self.headers["Content-Length"])
        content_bytes = self.rfile.read(content_length)
        content = json.loads(content_bytes)

        fork = content["state"]["fork"]

        self.server.spawn_subserver(fork)

        socket_path = runtime_dir / (fork + "." + str(os.getpid()) + ".sock")
        replaced_str = str(socket_path).replace("/", "%2F")
        self.server_url = f"http+unix://{replaced_str}/"

        response = Session().post(self.server_url, json=content, timeout=60)

        self.send_response(200)
        self.send_header("Content-type", "application/octet-stream")
        self.end_headers()

        self.wfile.write(response.text.encode("utf-8"))


class _UnixSocketHttpServer(socketserver.UnixStreamServer):
    last_response: Optional[float] = None
    processes: List[subprocess.Popen]

    def __init__(self, *args, **kwargs):
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.running_daemons = set()
        self.processes = []
        super().__init__(*args, **kwargs)

    def get_request(self) -> Tuple[Any, Any]:
        request, client_address = super().get_request()
        return request, ["local", 0]

    def finish_request(
        self, request: Union[socket, Tuple[bytes, socket]], client_address: Any
    ) -> None:
        try:
            super().finish_request(request, client_address)
        finally:
            self.last_response = time.monotonic()

    def check_timeout(self) -> None:
        while True:
            time.sleep(11.0)
            now = time.monotonic()
            last_response = self.last_response
            if last_response is None:
                self.last_response = now
            elif now - last_response > 60.0:
                self.shutdown()
                break

    def spawn_subserver(self, fork):
        if fork not in self.running_daemons:
            get_fork_resolution(fork).resolve(fork)

            uds_path = runtime_dir / (fork + "." + str(os.getpid()) + ".sock")
            self.processes.append(
                subprocess.Popen(
                    args=[
                        sys.argv[0],
                        "spawn-daemon",
                        "--state.fork",
                        fork,
                        "--uds",
                        str(uds_path),
                        "--timeout=0",
                    ]
                )
            )
            self.running_daemons.add(fork)
            time.sleep(1)

    def kill_subprocesses(self):
        for process in self.processes:
            process.terminate()
        sleep(1)
        for process in self.processes:
            process.kill()


class Daemon:
    """
    Converts HTTP requests into ethereum-spec-evm calls.
    """

    def __init__(self, uds) -> None:
        self.uds = uds

    def _run(self) -> int:
        # Perform cleanup when receiving SIGTERM
        signal.signal(signal.SIGTERM, lambda x, y: sys.exit())

        try:
            os.remove(self.uds)
        except IOError:
            pass

        with _UnixSocketHttpServer(self.uds, _EvmToolHandler) as server:
            server.timeout = 7.0
            timer = Thread(target=server.check_timeout, daemon=True)
            timer.start()

            try:
                server.serve_forever()
            finally:
                server.kill_subprocesses()

        return 0

    def run(self) -> int:
        """
        Execute the tool.
        """
        return self._run()
