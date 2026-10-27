"""Comfort helpers: smooth fades, a flash limiter for "Reduce flashing", and the strip's
color balance. Pure functions and small state classes, no I/O, so they're easy to test.
"""
import math
import sys

# Fade presets: name -> milliseconds
FADES = {"Off": 0, "Quick": 300, "Smooth": 700, "Slow": 1500}


def ease(t):
    """Smoothstep 0..1 -> 0..1: gentle start and end."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return t * t * (3 - 2 * t)


def fade_steps(start, end, n):
    """n colors easing from start toward end; the last one is exactly `end`. One step
    per strip update (~80 ms), so n = fade_ms / 80."""
    n = max(1, int(n))
    out = []
    for i in range(1, n + 1):
        e = ease(i / n)
        out.append(tuple(int(round(a + (b - a) * e)) for a, b in zip(start, end)))
    out[-1] = tuple(int(c) for c in end)
    return out


def apply_balance(rgb, balance):
    """Scale each channel by its gain (balance = (r, g, b) in 0..1). Used to correct a
    strip whose 'white' comes out pink or blue."""
    return tuple(max(0, min(255, int(round(c * g)))) for c, g in zip(rgb, balance))


class FlashLimiter:
    """Low-pass filter for color frames. A one-tick flash becomes a soft swell and a fast
    alternation melts into a steady glow, so nothing on the strip (or on the screen
    previews) flashes more than a few times a second. tau = how slow it reacts."""

    def __init__(self, tau=0.45):
        self.tau, self._prev = tau, None

    def reset(self):
        self._prev = None

    def step(self, rgb, dt):
        if self._prev is None:
            self._prev = tuple(float(c) for c in rgb)
        else:
            a = 1.0 - math.exp(-dt / self.tau)
            self._prev = tuple(p + (c - p) * a for p, c in zip(self._prev, rgb))
        return tuple(int(round(c)) for c in self._prev)


def luma(rgb):
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def max_flashes_per_second(frames, dt, jump=0.20, window=1.0):
    """Worst-case count of sudden brightening events (luminance up by `jump` or more
    within ~0.16 s) in any `window` seconds. Mirrors the 'no more than three flashes a
    second' accessibility rule closely enough to test against."""
    lum = [luma(f) for f in frames]
    k = max(1, round(0.16 / dt))
    events = [i * dt for i in range(1, len(lum))
              if lum[i] - min(lum[max(0, i - k):i]) >= jump
              and (i < 2 or lum[i - 1] - min(lum[max(0, i - 1 - k):i - 1]) < jump)]
    worst = 0
    for i, t0 in enumerate(events):
        worst = max(worst, sum(1 for t in events[i:] if t - t0 < window))
    return worst


def _selftest():
    """python rgbzap_comfort.py --selftest"""
    # fades: end exactly on target, monotone, eased (slow at both ends), right length
    s = fade_steps((0, 0, 0), (255, 100, 50), 9)
    assert len(s) == 9 and s[-1] == (255, 100, 50)
    assert all(a[0] <= b[0] for a, b in zip(s, s[1:])), "fade not monotone"
    assert s[0][0] < 255 / 9 and 255 - s[-2][0] < 255 / 9, s     # eased: gentle at both ends
    assert s[4][0] - s[3][0] > s[1][0] - s[0][0], "middle should move faster than the start"
    assert fade_steps((10, 10, 10), (10, 10, 10), 5)[0] == (10, 10, 10)
    assert fade_steps((5, 5, 5), (200, 0, 9), 1) == [(200, 0, 9)]
    assert fade_steps((255, 0, 0), (0, 0, 255), 6)[-1] == (0, 0, 255)
    assert FADES["Off"] == 0 and FADES["Smooth"] == 700
    # balance
    assert apply_balance((255, 255, 255), (1.0, 0.9, 0.8)) == (255, 230, 204)
    assert apply_balance((200, 100, 50), (1, 1, 1)) == (200, 100, 50)
    assert apply_balance((255, 255, 255), (0.0, 0.5, 1)) == (0, 128, 255)
    print("OK  comfort: eased fades land exactly on target, color balance scales channels")

    # flash limiter: a 6 Hz alternation and a lone one-tick strobe both stop being flashes
    dt = 0.08
    police = [(255, 0, 0) if (i // 2) % 2 == 0 else (0, 0, 0) for i in range(300)]
    assert max_flashes_per_second(police, dt) >= 3, "test signal should be flashing"
    lim = FlashLimiter()
    calm = [lim.step(f, dt) for f in police]
    assert max_flashes_per_second(calm, dt) == 0, "alternation should melt away"
    strobe = [(255, 255, 255) if i % 25 == 10 else (0, 0, 0) for i in range(500)]
    lim.reset()
    soft = [lim.step(f, dt) for f in strobe]          # starts dark, like a running effect
    assert max(luma(f) for f in soft) < 0.3 * max(luma(f) for f in strobe), "strobe not softened"
    # but it still follows slow changes: a step to white arrives (within a second or two)
    lim.reset()
    out = [lim.step((255, 255, 255), dt) for _ in range(40)]
    out2 = [lim.step((0, 0, 0), dt) for _ in range(40)]
    assert out[-1] == (255, 255, 255) and out2[-1][0] < 10, "limiter should still settle"
    assert out2[0][0] > 150, "a drop should ease down, not snap"
    # a calm 8 s breathing wave passes almost unchanged
    wave = [(int(127 + 127 * math.sin(2 * math.pi * i * dt / 8)),) * 3 for i in range(400)]
    lim.reset()
    smooth = [lim.step(f, dt) for f in wave]
    assert max(abs(a[0] - b[0]) for a, b in zip(wave[100:], smooth[100:])) < 80
    print("OK  comfort: flash limiter removes fast flashing, keeps slow changes")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
