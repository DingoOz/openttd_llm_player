"""Launch and control one headless OpenTTD dedicated server per episode."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .admin import AdminClient, AdminError
from .bridge import Bridge, BridgeError
from .config import OPENTTD_BIN, SCRIPTS_DIR, Scenario

ADMIN_PASSWORD = "ottd-bench"   # the admin port is bound to loopback only


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenTTDServer:
    """One isolated OpenTTD instance. Everything it reads or writes lives under `work_dir`."""

    def __init__(self, work_dir: Path, scenario: Scenario):
        self.work_dir = Path(work_dir).resolve()   # OpenTTD is started with this as its cwd
        self.scenario = scenario
        self.proc: subprocess.Popen | None = None
        self.admin: AdminClient | None = None
        self.bridge: Bridge | None = None
        self.info: dict = {}
        self._log = None

    # ---------------------------------------------------------------- config

    def _write_config(self, port: int, admin_port: int) -> Path:
        g = self.scenario.game
        bits = g.map_size.bit_length() - 1
        sections: dict[str, dict] = {
            # Without a current ini_version OpenTTD treats the file as pre-12.0, ignores
            # secrets.cfg (so the admin port never opens) and applies legacy value conversions.
            "version": {"ini_version": 7},
            "misc": {"resolution": "1024,768"},
            "network": {
                "server_port": port,
                "server_admin_port": admin_port,
                "server_game_type": "local",
                "allow_insecure_admin_login": "true",
                "min_active_clients": 0,
            },
            "game_creation": {
                "map_x": bits,
                "map_y": bits,
                "starting_year": g.start_year,
                "landscape": g.climate,
                "amount_of_rivers": g.rivers,
            },
            "difficulty": {
                "number_towns": g.towns,
                "industry_density": g.industries,
                "terrain_type": g.terrain,
                "quantity_sea_lakes": g.sea,
                "max_loan": g.max_loan,
                "vehicle_breakdowns": g.breakdowns,
                "disasters": "false",
            },
            # Level 3 lets the game script build while the game is paused.
            "construction": {"command_pause_level": 3},
            "script": {"script_max_opcode_till_suspend": 250000, "script_max_memory_megabytes": 1024},
            "ai": {"ai_in_multiplayer": "true"},
            "gui": {"autosave_interval": 0},
            "game_scripts": {"LLMBridge": ""},
        }
        for dotted, value in g.extra_settings.items():
            section, key = dotted.split(".", 1)
            sections.setdefault(section, {})[key] = value

        def render(secs: dict) -> str:
            return "".join(f"[{name}]\n" + "".join(f"{k} = {v}\n" for k, v in body.items()) + "\n" for name, body in secs.items())

        cfg = self.work_dir / "openttd.cfg"
        cfg.write_text(render(sections))
        (self.work_dir / "secrets.cfg").write_text(render({"network": {"admin_password": ADMIN_PASSWORD}}))
        (self.work_dir / "private.cfg").write_text(render({"network": {
            "server_name": "ottd-bench",
            "client_name": "ottd-bench",
            "use_relay_service": "never",
            "participate_survey": "no",
        }}))
        return cfg

    # ------------------------------------------------------------- lifecycle

    def start(self, timeout: float = 90.0) -> Bridge:
        if not OPENTTD_BIN.exists():
            raise FileNotFoundError(f"{OPENTTD_BIN} is missing; run scripts/setup.sh first")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("ai", "game"):
            dst = self.work_dir / sub
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(SCRIPTS_DIR / sub, dst)

        port, admin_port = _free_port(), _free_port()
        cfg = self._write_config(port, admin_port)
        home = self.work_dir / "home"
        home.mkdir(exist_ok=True)
        # A private HOME keeps the user's own OpenTTD content (NewGRFs, scripts) out of the run.
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / "data"), XDG_CONFIG_HOME=str(home / "config"))
        self._log = open(self.work_dir / "server.log", "w")
        self.proc = subprocess.Popen(
            # -b must follow -D: -D resets the blitter to "null", which makes screenshots impossible.
            [str(OPENTTD_BIN), "-D", f"127.0.0.1:{port}", "-b", "8bpp-optimized",
             "-c", str(cfg), "-G", str(self.scenario.game.seed), "-x"],
            stdin=subprocess.PIPE, stdout=self._log, stderr=subprocess.STDOUT, env=env, cwd=self.work_dir,
        )

        deadline = time.monotonic() + timeout
        self.admin = AdminClient("127.0.0.1", admin_port, ADMIN_PASSWORD)
        while True:
            self._check_alive()
            try:
                self.admin.connect(timeout=5)
                break
            except (OSError, AdminError, TimeoutError):
                if time.monotonic() > deadline:
                    raise RuntimeError(f"OpenTTD admin port never came up; see {self.work_dir / 'server.log'}")
                time.sleep(0.3)

        self.bridge = Bridge(self.admin)
        self._wait_for("ping", deadline)
        self.admin.rcon("start_ai LLMIdle")
        self.info = self._wait_for("init", deadline, company=0, name="LLM Transport")
        return self.bridge

    def _wait_for(self, cmd: str, deadline: float, **args) -> dict:
        """Retry a bridge command until it succeeds (the script and company come up asynchronously)."""
        last = "no reply"
        while time.monotonic() < deadline:
            self._check_alive()
            try:
                res = self.bridge.call(cmd, timeout=3, **args)
                if res.get("ok"):
                    return res
                last = res.get("error", last)
            except BridgeError as e:
                last = str(e).splitlines()[0]
            time.sleep(0.3)
        raise RuntimeError(f"game script never answered '{cmd}' ({last}); see {self.work_dir / 'server.log'}")

    def _check_alive(self) -> None:
        if self.proc is not None and self.proc.poll() is not None:
            raise RuntimeError(f"OpenTTD exited with code {self.proc.returncode}; see {self.work_dir / 'server.log'}")

    def rcon(self, command: str, timeout: float = 60.0) -> list[str]:
        return self.admin.rcon(command, timeout=timeout)

    def screenshot(self, kind: str, name: str) -> Path:
        """kind: 'giant' (whole map, isometric) or 'minimap' (one pixel per tile)."""
        path = self.work_dir / "screenshot" / f"{name}.png"
        path.unlink(missing_ok=True)
        self.rcon(f"screenshot {kind} {name}")
        # The console command only queues the screenshot; it is written at the top of the next
        # main-loop iteration, before any further network input is read. A second rcon round
        # trip therefore returns only after the file is complete.
        self.rcon("echo sync")
        if not path.exists():
            raise RuntimeError(f"OpenTTD did not write {path.name}; see {self.work_dir / 'server.log'}")
        return path

    def save(self, name: str, timeout: float = 30.0) -> Path:
        path = self.work_dir / "save" / f"{name}.sav"
        path.unlink(missing_ok=True)
        self.rcon(f"save {name}")
        # Saving runs on a worker thread; wait until the file has stopped growing.
        deadline = time.monotonic() + timeout
        last = -1
        while time.monotonic() < deadline:
            size = path.stat().st_size if path.exists() else -1
            if size > 0 and size == last:
                return path
            last = size
            time.sleep(0.25)
        raise RuntimeError(f"OpenTTD did not write {path.name}")

    def stop(self) -> None:
        asked_to_quit = False
        if self.admin is not None:
            if self.admin.sock is not None:
                try:
                    self.admin._send(5, b"quit\0")  # rcon without waiting: the server exits before replying
                    asked_to_quit = True
                except OSError:
                    pass
            self.admin.close()
            self.admin = None
        if self.proc is not None:
            if not asked_to_quit:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def __enter__(self) -> "OpenTTDServer":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
