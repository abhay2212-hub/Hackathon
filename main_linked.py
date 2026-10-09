"""Your teammate's main.py (phone CCTV + YOLO) connected to the simulation.

Same live detection window and TRAFFIC ALERT as his version, plus:
  - tracking, so each vehicle is counted once
  - every WINDOW seconds the counts go to the simulation (yolo_counts.json)

    python main_linked.py --source 192.168.1.7              # PHONE camera: the IP shown in the DroidCam app
    python main_linked.py                                   # phone stream at PHONE_STREAM below
    python main_linked.py --source video.mp4                # a video file instead (loops)
    python main_linked.py --source usb                      # PHONE by USB (DroidCam PC client connected)
    python main_linked.py --source 1                        # a camera by number (0 = laptop webcam, NOT the phone)

The camera watches ONE road of the junction. Which SUMO road that is: settings.py ->
    CAMERA_JUNCTION = "J1"
    CAMERA_ROADS = {"W": "E0"}
"""
import argparse
import os
import time

import cv2
from ultralytics import YOLO

from yolo_link import CameraLink

PHONE_STREAM = "http://172.10.23.162:4747/video"     # DroidCam phone address (change to yours)
ROAD = "W"                                            # which road of the junction the camera watches
VEHICLE_CLASSES = [1, 2, 3, 5, 7]                     # COCO: bicycle, car, motorcycle, bus, truck
ALERT_COUNT = 3                                       # more vehicles than this in view = TRAFFIC ALERT
HERE = os.path.dirname(os.path.abspath(__file__))


def phone_url(src):
    """'192.168.1.7' or '192.168.1.7:4747'  ->  'http://192.168.1.7:4747/video' (DroidCam phone address)."""
    s = str(src).strip()
    host = s.split(":")[0]
    if host.count(".") == 3 and host.replace(".", "").isdigit():
        return f"http://{s if ':' in s else s + ':4747'}/video"
    return s


def open_webcam(i):
    """Webcam number i. On Windows DirectShow is used: it sees the DroidCam USB camera."""
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(i)
    if not cap.isOpened():
        cap = cv2.VideoCapture(i)
    return cap


def find_usb_phone():
    """DroidCam over USB = an extra webcam. 0 is the laptop's own camera, so try 1, 2, 3... first."""
    for i in (1, 2, 3, 4, 5, 0):
        cap = open_webcam(i)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        if ok and frame is not None:
            print(f"Using camera number {i}. If this is NOT the phone, run with --source {i + 1}", flush=True)
            cap.release()
            return i
        cap.release()
    raise SystemExit("No phone camera found. Open the DroidCam PC client, connect by USB and press Start, then try again.")


def open_source(src):
    if str(src).lower() == "usb":
        src = find_usb_phone()
    src = phone_url(src)
    cap = open_webcam(int(src)) if str(src).isdigit() else cv2.VideoCapture(src)
    if not cap.isOpened():
        raise SystemExit(f"Could not open {src}.\n"
                         "  1. Open the DroidCam app on the phone and keep it open (screen on).\n"
                         "  2. Phone and laptop on the SAME Wi-Fi / hotspot.\n"
                         "  3. Use the IP shown in the app:  python main_linked.py --source 192.168.x.x\n"
                         "  4. Close the DroidCam PC client and any browser tab showing the camera (only one viewer allowed).")
    print("Camera:", src, flush=True)
    return cap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=PHONE_STREAM)
    ap.add_argument("--model", default=os.path.join(HERE, "yolo11n.pt"))
    ap.add_argument("--road", default=ROAD)
    ap.add_argument("--window", type=int, default=10, help="seconds between updates to the simulation")
    ap.add_argument("--server", default=None, help="http://<simulation-laptop-IP>:8600 (or cloud server) if SUMO runs on another laptop")
    ap.add_argument("--key", default=None, help="CONTROL_KEY of the server (cloud)")
    ap.add_argument("--conf", type=float, default=0.20, help="lower = finds more (bikes), higher = fewer false boxes")
    ap.add_argument("--size", type=int, default=640, help="960 or 1280 finds small/far bikes better (slower)")
    a = ap.parse_args()

    print("Loading YOLO model...", flush=True)
    model = YOLO(a.model)
    names = model.names
    cap = open_source(a.source)
    is_file = os.path.exists(str(a.source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    link = CameraLink(server=a.server, key=a.key, window_s=a.window, roads=(a.road,))
    seen, last_pos, frame_no, t0 = set(), {}, 0, time.time()
    print(f"Connected. Counting road {a.road}, sending to the simulation every {a.window} s. Q = stop", flush=True)

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                if is_file:                                   # loop a video file
                    cap.release()
                    cap = open_source(a.source)
                    seen.clear()
                    continue
                print("No frame received. Check the phone stream.")
                break
            frame_no += 1
            start = time.perf_counter()
            result = model.track(frame, persist=True, tracker="bytetrack.yaml", imgsz=a.size, conf=a.conf,
                                 classes=VEHICLE_CLASSES, verbose=False)[0]
            latency = (time.perf_counter() - start) * 1000

            in_view, waiting = 0, 0
            boxes = result.boxes
            if boxes is not None and boxes.id is not None:
                for (x1, y1, x2, y2), tid, c in zip(boxes.xyxy.tolist(), boxes.id.int().tolist(), boxes.cls.int().tolist()):
                    in_view += 1
                    if tid not in seen:                       # a new vehicle on this road
                        seen.add(tid)
                        link.vehicle_passed(a.road, names[c])
                    p = ((x1 + x2) / 2, y2)
                    q = last_pos.get(tid)
                    if q and abs(p[0] - q[0]) + abs(p[1] - q[1]) < 2:   # hardly moved since last frame = waiting
                        waiting += 1
                    last_pos[tid] = p
            link.queue(a.road, waiting)
            sent = link.tick(frame_no / fps if is_file else None)

            if is_file:                                       # play a file at normal speed (same pace as SUMO)
                ahead = t0 + frame_no / fps - time.time()
                if ahead > 0:
                    time.sleep(ahead)

            display = result.plot()
            alert = in_view > ALERT_COUNT
            cv2.rectangle(display, (10, 10), (470, 140), (25, 25, 25), -1)
            cv2.putText(display, f"Vehicles detected: {in_view}  (waiting {waiting})", (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
            cv2.putText(display, "TRAFFIC ALERT" if alert else "NO TRAFFIC ALERT", (20, 75),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255) if alert else (0, 200, 0), 2)
            cv2.putText(display, f"Road {a.road}: {link.counts[a.road]['count']} counted this window"
                                 + ("  -> SENT" if sent else ""), (20, 105),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 1)
            cv2.putText(display, f"Inference: {latency:.0f} ms", (20, 130),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.imshow("Phone CCTV - Live YOLO Detection", display)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
        print("Detection stopped.", flush=True)


if __name__ == "__main__":
    main()
