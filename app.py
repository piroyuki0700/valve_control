#!/usr/bin/env python3
"""Two normally-closed electric ball valve controller."""

import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template, request

try:
    from gpiozero import OutputDevice
except ImportError:
    OutputDevice = None

GPIO_VALVE_1 = 17
GPIO_VALVE_2 = 27
SETTINGS_FILE = "valve_settings.json"
LOG_FILE = "valve_control.log"

DEFAULT_SETTINGS = {
    "control_enabled": True,
    "valves": {
        "1": {
            "schedule_1_time": "08:00",
            "schedule_1_duration": 0,
            "schedule_2_time": "20:00",
            "schedule_2_duration": 0,
        },
        "2": {
            "schedule_1_time": "08:00",
            "schedule_1_duration": 0,
            "schedule_2_time": "20:00",
            "schedule_2_duration": 0,
        },
    },
}

logger = logging.getLogger("ValveControl")


class DummyOutputDevice:
    """Fallback device for development machines without GPIO access."""

    def __init__(self, pin):
        self.pin = pin
        self.is_active = False

    def on(self):
        self.is_active = True

    def off(self):
        self.is_active = False

    def close(self):
        self.off()


def create_output_device(pin):
    if OutputDevice is not None:
        try:
            # OutputDevice uses active_high=True by default: HIGH opens the valve.
            return OutputDevice(pin, active_high=True, initial_value=False)
        except Exception as error:
            logger.warning("GPIO %s unavailable; using dummy output: %s", pin, error)
    return DummyOutputDevice(pin)


def copy_defaults():
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            loaded = json.load(settings_file)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        logger.warning("設定読み込み失敗。初期設定を使用します: %s", error)
        return copy_defaults()

    settings = copy_defaults()
    if isinstance(loaded, dict):
        settings["control_enabled"] = bool(loaded.get("control_enabled", True))
        loaded_valves = loaded.get("valves", {})
        if isinstance(loaded_valves, dict):
            for valve_id in settings["valves"]:
                valve_settings = loaded_valves.get(valve_id, {})
                if isinstance(valve_settings, dict):
                    settings["valves"][valve_id].update(valve_settings)
    return normalize_settings(settings)


def normalize_settings(settings):
    normalized = copy_defaults()
    normalized["control_enabled"] = bool(settings.get("control_enabled", True))
    for valve_id in normalized["valves"]:
        source = settings.get("valves", {}).get(valve_id, {})
        for slot in (1, 2):
            time_key = f"schedule_{slot}_time"
            duration_key = f"schedule_{slot}_duration"
            time_value = source.get(time_key, normalized["valves"][valve_id][time_key])
            try:
                datetime.strptime(str(time_value), "%H:%M")
                normalized["valves"][valve_id][time_key] = str(time_value)
            except (TypeError, ValueError):
                raise ValueError(f"{time_key} は HH:MM 形式で指定してください")
            try:
                duration = int(source.get(duration_key, 0))
            except (TypeError, ValueError):
                raise ValueError(f"{duration_key} は整数で指定してください")
            if duration < 0 or duration > 1440:
                raise ValueError(f"{duration_key} は0から1440の範囲で指定してください")
            normalized["valves"][valve_id][duration_key] = duration
    return normalized


def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as settings_file:
        json.dump(settings, settings_file, indent=4, ensure_ascii=False)


def is_scheduled_open(valve_settings, now):
    current_minute = now.hour * 60 + now.minute
    for slot in (1, 2):
        duration = valve_settings[f"schedule_{slot}_duration"]
        if duration == 0:
            continue
        start = datetime.strptime(
            valve_settings[f"schedule_{slot}_time"], "%H:%M"
        ).hour * 60 + datetime.strptime(
            valve_settings[f"schedule_{slot}_time"], "%H:%M"
        ).minute
        elapsed = (current_minute - start) % (24 * 60)
        if elapsed < duration:
            return True
    return False


class ValveController:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.stop_lock = threading.Lock()
        self.stopped = False
        self.settings = load_settings()
        self.devices = {
            "1": create_output_device(GPIO_VALVE_1),
            "2": create_output_device(GPIO_VALVE_2),
        }
        self.manual_state = {"1": False, "2": False}
        self.manual_until = {"1": None, "2": None}
        self.thread = threading.Thread(target=self._control_loop, daemon=True)
        self.thread.start()

    def set_output(self, valve_id, should_open):
        device = self.devices[valve_id]
        if should_open:
            device.on()
        else:
            device.off()

    def apply_schedule(self):
        with self.lock:
            now = datetime.now()
            if not self.settings.get("control_enabled", True):
                for valve_id in self.devices:
                    if self.devices[valve_id].is_active:
                        self.set_output(valve_id, False)
                return
            for valve_id, device in self.devices.items():
                scheduled = is_scheduled_open(self.settings["valves"][valve_id], now)
                manual_active = self.manual_until[valve_id] is not None
                if manual_active and now.timestamp() >= self.manual_until[valve_id]:
                    self.manual_until[valve_id] = None
                    manual_active = False
                should_open = scheduled or manual_active
                if should_open != bool(device.is_active):
                    self.set_output(valve_id, should_open)

    def manual_control(self, valve_id, should_open, max_duration=None):
        with self.lock:
            if should_open:
                if max_duration is None:
                    raise ValueError("最大開放時間を指定してください")
                try:
                    max_duration = int(max_duration)
                except (TypeError, ValueError):
                    raise ValueError("最大開放時間は整数で指定してください")
                if max_duration < 1 or max_duration > 1440:
                    raise ValueError("最大開放時間は1から1440分の範囲で指定してください")
                self.manual_until[valve_id] = datetime.now().timestamp() + max_duration * 60
            else:
                self.manual_until[valve_id] = None
            self.apply_schedule()

    def update_settings(self, new_settings):
        normalized = normalize_settings(new_settings)
        with self.lock:
            self.settings = normalized
            save_settings(normalized)
            self.apply_schedule()

    def status(self):
        with self.lock:
            return {
                "control_enabled": self.settings.get("control_enabled", True),
                "valves": {
                    valve_id: {
                        "open": bool(device.is_active),
                        "manual_remaining": max(
                            0,
                            int(self.manual_until[valve_id] - datetime.now().timestamp()),
                        ) if self.manual_until[valve_id] is not None else 0,
                        "gpio": GPIO_VALVE_1 if valve_id == "1" else GPIO_VALVE_2,
                        **self.settings["valves"][valve_id],
                    }
                    for valve_id, device in self.devices.items()
                },
            }

    def _control_loop(self):
        while not self.stop_event.wait(1):
            try:
                self.apply_schedule()
            except Exception:
                logger.exception("バルブ自動制御中に例外が発生しました")

    def stop(self):
        with self.stop_lock:
            if self.stopped:
                return
            self.stopped = True
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
        with self.lock:
            for device in self.devices.values():
                device.off()
                close = getattr(device, "close", None)
                if close is not None:
                    close()


app = Flask(__name__)
controller = ValveController()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    if request.method == "GET":
        with controller.lock:
            return jsonify(controller.settings)
    try:
        controller.update_settings(request.get_json(silent=True) or {})
    except (TypeError, ValueError, OSError) as error:
        logger.warning("設定更新失敗: %s", error)
        return jsonify({"status": "error", "message": str(error)}), 400
    return jsonify({"status": "success"})


@app.route("/api/status")
def status_api():
    return jsonify(controller.status())


@app.route("/api/valve/<valve_id>/manual", methods=["POST"])
def manual_api(valve_id):
    if valve_id not in controller.devices:
        return jsonify({"status": "error", "message": "不正なバルブ番号です"}), 404
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("open", "close"):
        return jsonify({"status": "error", "message": "action は open または close です"}), 400
    try:
        controller.manual_control(
            valve_id,
            action == "open",
            data.get("max_duration") if action == "open" else None,
        )
    except ValueError as error:
        return jsonify({"status": "error", "message": str(error)}), 400
    except Exception as error:
        logger.exception("手動操作失敗")
        return jsonify({"status": "error", "message": str(error)}), 500
    return jsonify({"status": "success", **controller.status()})


def shutdown(signum=None, frame=None):
    logger.info("停止シグナルを受信しました: %s", signum)
    sys.exit(0)


if __name__ == "__main__":
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    finally:
        controller.stop()
