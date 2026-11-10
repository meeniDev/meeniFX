"""Download the official MediaPipe hand-landmarker model used by Hand Tracker.

The model is intentionally not committed: it is a 7.5 MB binary weight.  Run this
once after installing dependencies, or point MEENIFX_HAND_MODEL at a copy.

    python tools/fetch_hand_model.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meenifx.config import ASSETS_DIR  # noqa: E402

URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
       "hand_landmarker/float16/1/hand_landmarker.task")
SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"
DESTINATION = os.path.join(ASSETS_DIR, "models", "hand_landmarker.task")


def main() -> int:
    if os.path.isfile(DESTINATION):
        with open(DESTINATION, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        if digest == SHA256:
            print(f"Hand model already present: {DESTINATION}")
            return 0
        print(f"Existing model has the wrong checksum; replacing {DESTINATION}")
    os.makedirs(os.path.dirname(DESTINATION), exist_ok=True)
    temporary = DESTINATION + ".part"
    print(f"Downloading {URL}")
    try:
        urllib.request.urlretrieve(URL, temporary)
    except Exception as exc:
        if os.path.exists(temporary):
            os.remove(temporary)
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    with open(temporary, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    if digest != SHA256:
        os.remove(temporary)
        print(f"Checksum mismatch: expected {SHA256}, got {digest}", file=sys.stderr)
        return 1
    os.replace(temporary, DESTINATION)
    print(f"Saved {DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
