"""Screen-sync quality helpers: ignore black bars, and sample just part of the screen.
Pure numpy functions on small arrays, so they are fast and easy to test.
"""
import sys

import numpy as np

AREAS = ("Whole screen", "Edges", "Center", "Left", "Right", "Top", "Bottom")


def crop_black_bars(arr, thresh=18, max_frac=0.15, min_frac=0.02, step=8):
    """Cut letterbox / pillarbox bars off an HxWx(3|4) frame (returns a view).

    A bar is a run of rows (or columns) at the edge that is black all the way across.
    At most max_frac per side is removed: cinema bars are ~12.5%, so this can't be
    fooled into cropping a dark scene down to one bright object. Bars thinner than
    min_frac are ignored (they are rounding, not bars)."""
    h, w = arr.shape[:2]
    dark = arr[::step, ::step, :3].max(axis=2) <= thresh

    def bar(black, size):
        idx = np.flatnonzero(~black)
        lead, trail = (idx[0], black.size - 1 - idx[-1]) if idx.size else (black.size, black.size)
        lead = lead if lead >= min_frac * black.size else 0
        trail = trail if trail >= min_frac * black.size else 0
        # the bar is measured on a shrunken copy, so it can end up to `step` pixels past
        # what we saw: trim one extra step so no black edge pixels are left behind
        cap = int(size * max_frac)                        # per side, margin included
        lead_px = min(lead * step + step, cap) if lead else 0
        trail_px = min(trail * step + step, cap) if trail else 0
        return lead_px, size - trail_px

    y0, y1 = bar(dark.mean(axis=1) >= 0.995, h)         # rows that are black end to end
    x0, x1 = bar(dark.mean(axis=0) >= 0.995, w)         # columns likewise
    return arr[y0:y1, x0:x1]


def select_area(sub, area, band=0.25):
    """sub: small HxWx3 array -> (N, 3) pixels from the chosen part of the screen.
    'Edges' is the border band all round (what bias lighting should match); 'Center'
    the middle half; Left/Right/Top/Bottom a band on that side."""
    h, w = sub.shape[:2]
    bh, bw = max(1, int(h * band)), max(1, int(w * band))
    if area == "Left":
        return sub[:, :bw].reshape(-1, 3)
    if area == "Right":
        return sub[:, w - bw:].reshape(-1, 3)
    if area == "Top":
        return sub[:bh].reshape(-1, 3)
    if area == "Bottom":
        return sub[h - bh:].reshape(-1, 3)
    if area == "Center":
        return sub[h // 4:h - h // 4, w // 4:w - w // 4].reshape(-1, 3)
    if area == "Edges":
        mask = np.ones((h, w), dtype=bool)
        mask[bh:h - bh, bw:w - bw] = False
        return sub[mask]
    return sub.reshape(-1, 3)                            # "Whole screen" and anything unknown


def _selftest():
    """python rgbzap_sync.py --selftest"""
    H, W = 1080, 1920
    gray = np.full((H, W, 4), 120, dtype=np.uint8)
    # 2.39:1 film on a 16:9 screen: ~12.8% black bars top and bottom
    film = gray.copy()
    bar = int(H * 0.128)
    film[:bar] = 0
    film[H - bar:] = 0
    letter = crop_black_bars(film)
    assert abs((H - letter.shape[0]) - 2 * bar) <= 32 and letter.shape[1] == W, letter.shape
    assert letter.min() > 100, "cropped frame still contains bar pixels"
    assert crop_black_bars(film[:, :, :3]).shape[:2] == letter.shape[:2], "3-channel frames too"
    # 4:3 on 16:9: pillarbox bars left and right (12.5%), capped at 15% per side
    pill = gray.copy()
    side = int(W * 0.125)
    pill[:, :side] = 0
    pill[:, W - side:] = 0
    c = crop_black_bars(pill)
    assert abs((W - c.shape[1]) - 2 * side) <= 32 and c.shape[0] == H, c.shape
    assert c.min() > 100, "pillarbox: bar pixels left behind"
    # a frame with no bars is left alone
    assert crop_black_bars(gray).shape == gray.shape
    # a tiny bar (rounding) is left alone; a huge black area is capped, never eaten whole
    thin = gray.copy()
    thin[:6] = 0
    assert crop_black_bars(thin).shape == thin.shape
    black = np.zeros((H, W, 4), dtype=np.uint8)
    cb = crop_black_bars(black)
    assert cb.shape[0] >= H * 0.69 and cb.shape[1] >= W * 0.69, cb.shape
    # a small bright object on black is NOT cropped down to the object
    spot = np.zeros((H, W, 4), dtype=np.uint8)
    spot[500:580, 900:1000] = 255
    cs = crop_black_bars(spot)
    assert cs.shape[0] >= H * 0.69 and cs.shape[1] >= W * 0.69, cs.shape
    # subtitles burnt into the bottom bar keep that bar (those rows aren't black end to end)
    subs = film.copy()
    subs[H - bar // 2:H - bar // 2 + 6, 700:1200] = 255
    assert crop_black_bars(subs).shape[0] > letter.shape[0] + 40
    print("OK  sync: letterbox + pillarbox cropped, thin/none/black/spot frames left sane")

    # areas: a red frame border around a green center, left red / right blue halves
    img = np.zeros((90, 160, 3), dtype=np.uint8)
    img[...] = (0, 0, 255)                                # BGR red border
    img[24:66, 42:118] = (0, 255, 0)                      # green center (inside the middle half)
    edges = select_area(img, "Edges")
    center = select_area(img, "Center")
    assert edges.mean(axis=0)[2] > 200 and edges.mean(axis=0)[1] < 60, "edges should be red"
    assert center.mean(axis=0)[1] > 140, "center should be mostly green"
    halves = np.zeros((90, 160, 3), dtype=np.uint8)
    halves[:, :80] = (0, 0, 255)
    halves[:, 80:] = (255, 0, 0)
    assert select_area(halves, "Left").mean(axis=0)[2] > 250
    assert select_area(halves, "Right").mean(axis=0)[0] > 250
    tb = np.zeros((90, 160, 3), dtype=np.uint8)
    tb[:45] = (0, 255, 0)
    tb[45:] = (255, 0, 255)
    assert select_area(tb, "Top").mean(axis=0)[1] > 250
    assert select_area(tb, "Bottom").mean(axis=0)[2] > 250
    assert select_area(img, "Whole screen").shape[0] == 90 * 160
    assert select_area(img, "Nonsense").shape[0] == 90 * 160, "unknown area should be whole screen"
    assert select_area(np.zeros((2, 2, 3), np.uint8), "Edges").shape[1] == 3, "tiny frames work"
    print("OK  sync: edges/center/left/right/top/bottom pick the right pixels")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
