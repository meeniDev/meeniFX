# Hand-tracking fixtures

Two real photographs are used for end-to-end MediaPipe landmark checks. They are
committed at modest resolution and are not test-generated images.

| File | Source | Author | License |
| --- | --- | --- | --- |
| `human_hand.jpg` | [Human Hand.JPG](https://commons.wikimedia.org/wiki/File:Human_Hand.JPG) | Jason Auerbach | Public domain (PD-self) |
| `two_hands.jpg` | [071228 human hands.JPG](https://commons.wikimedia.org/wiki/File:071228_human_hands.JPG) | Luisfi | CC BY-SA 3.0 |

The two-hand image is used unmodified except for the download itself; attribution is
provided here as required by CC BY-SA 3.0. No model training or redistribution of
derived weights is involved.

The hand model itself is not stored in the repository. Fetch it with:

```bash
python tools/fetch_hand_model.py
```

Then run the real-inference test:

```bash
python -m pytest tests/test_hand_landmarks_real.py -q
```
