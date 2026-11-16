#!/usr/bin/env python3
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Raspberry Pi front-panel service. SPDX-License-Identifier: MIT

import argparse
import json
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time


def read_object(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def display_lines(data_dir, page):
    ip_result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2, check=False)
    address = next((value for value in ip_result.stdout.split() if "." in value), "No IPv4 address")
    content = read_object(data_dir / "apply-state.json")
    maps = read_object(data_dir / "map-sync-state.json")
    usage = shutil.disk_usage("/")
    content_status = "Installing content" if content.get("action") == "full" else "Working on content"
    pages = (
        "PrepperCache",
        content_status if content.get("status") == "running" else "Content idle",
        "Working on maps" if maps.get("status") == "running" else "Maps idle",
        f"{usage.free / 1024**3:.1f} GB free",
    )
    return address, pages[page % len(pages)]


def run(data_dir, stop):
    settings_path = data_dir / "gpio-settings.json"
    button = screen = None
    pin = address = None
    armed = False
    pressed_at = None
    page = 0
    next_settings = next_frame = next_button_retry = next_screen_retry = 0
    try:
        while not stop.wait(0.05):
            now = time.monotonic()
            if now >= next_settings:
                next_settings = now + 1
                settings = read_object(settings_path)
                new_pin = settings.get("shutdown_gpio") if settings.get("shutdown_enabled") is True else None
                new_address = settings.get("oled_address") if settings.get("oled_enabled") is True else None
                if new_pin != pin:
                    if button:
                        button.close()
                    button, pin = None, new_pin
                    armed, pressed_at, next_button_retry = False, None, 0
                if new_address != address:
                    if screen:
                        try:
                            screen.cleanup()
                        except OSError as exc:
                            print(f"OLED cleanup: {exc}", flush=True)
                    screen, address, next_screen_retry = None, new_address, 0

            if pin is not None and button is None and now >= next_button_retry:
                try:
                    from gpiozero import Button
                    button = Button(pin, pull_up=True, bounce_time=0.05)
                except (ImportError, OSError, RuntimeError, ValueError) as exc:
                    print(f"Shutdown button GPIO {pin}: {exc}", flush=True)
                    next_button_retry = now + 15

            if button:
                if not button.is_pressed:
                    armed, pressed_at = True, None
                elif armed and pressed_at is None:
                    pressed_at = now
                elif armed and now - pressed_at >= 2:
                    armed = False
                    latest = read_object(settings_path)
                    if latest.get("shutdown_enabled") is True and latest.get("shutdown_gpio") == pin:
                        print(f"GPIO {pin} held. Requesting orderly power-off.", flush=True)
                        subprocess.run(["systemctl", "--no-block", "poweroff"], check=False, timeout=5)

            if address is not None and screen is None and now >= next_screen_retry:
                try:
                    from luma.core.interface.serial import i2c
                    from luma.oled.device import ssd1306
                    screen = ssd1306(i2c(port=1, address=int(address, 16)), width=128, height=32)
                    next_frame = 0
                except Exception as exc:
                    # Luma raises its own device errors for missing buses and displays.
                    print(f"OLED {address} unavailable: {exc}", flush=True)
                    next_screen_retry = now + 15

            if screen and now >= next_frame:
                from luma.core.render import canvas
                from PIL import ImageFont
                try:
                    first, second = display_lines(data_dir, page)
                    with canvas(screen) as draw:
                        font = ImageFont.load_default()
                        draw.text((2, 1), first, fill="white", font=font)
                        draw.text((2, 17), second, fill="white", font=font)
                    page += 1
                    next_frame = now + 4
                except Exception as exc:
                    print(f"OLED update: {exc}", flush=True)
                    try:
                        screen.cleanup()
                    except OSError:
                        pass
                    screen, next_screen_retry = None, now + 15
    finally:
        if button:
            button.close()
        if screen:
            try:
                screen.cleanup()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(args.data_dir, stop)


if __name__ == "__main__":
    main()
