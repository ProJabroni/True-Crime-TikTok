#!/usr/bin/env python3
"""Original, copyright-free background music: a low, ominous ambient drone.

Synthesized from scratch with numpy for every video (different key and texture each time),
so there is nothing to license and nothing for TikTok's copyright checks to match.
"""
import wave

import numpy as np

SR = 44100
# Low root notes (Hz) the drone can be built on: A1, B1, C2, D2, E2
ROOTS = [55.00, 61.74, 65.41, 73.42, 82.41]


def _lfo(t, rate, phase, depth, base=1.0):
    return base - depth + depth * (0.5 + 0.5 * np.sin(2 * np.pi * rate * t + phase))


def _lowpass(x, cutoff):
    """Gentle low-pass filter done in the frequency domain (fast, no extra libraries)."""
    size = 1 << int(np.ceil(np.log2(len(x))))
    f = np.fft.rfftfreq(size, 1 / SR)
    h = 1 / np.sqrt(1 + (f / cutoff) ** 4)  # 2nd-order roll-off
    return np.fft.irfft(np.fft.rfft(x, size) * h, size)[:len(x)]


def _reverb(x, rng, seconds=2.8, mix=0.35):
    """Convolve with a decaying-noise impulse response: a big, dark room."""
    n = int(seconds * SR)
    ir = rng.standard_normal(n) * np.exp(-np.linspace(0, 6, n))
    ir = _lowpass(ir, 2500.0)
    ir /= np.sqrt(np.sum(ir ** 2))
    size = 1 << int(np.ceil(np.log2(len(x) + n)))
    wet = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[:len(x)]
    return (1 - mix) * x + mix * wet / (np.max(np.abs(wet)) + 1e-9) * np.max(np.abs(x))


def make_music(seconds, path, seed=0):
    rng = np.random.default_rng(seed)
    n = int((seconds + 1) * SR)
    t = np.arange(n) / SR
    root = rng.choice(ROOTS)

    def voice(freq, amp, rate):
        detune = rng.uniform(0.15, 0.45)
        ph = rng.uniform(0, 2 * np.pi, 3)
        tone = (np.sin(2 * np.pi * freq * t + ph[0])
                + 0.6 * np.sin(2 * np.pi * (freq + detune) * t + ph[1])
                + 0.6 * np.sin(2 * np.pi * (freq - detune) * t + ph[2]))
        return amp * tone * _lfo(t, rate, rng.uniform(0, 6.28), 0.6)

    left = np.zeros(n)
    right = np.zeros(n)
    # Minor chord drone: root, minor third, fifth, octave + a sub an octave down.
    for ratio, amp in [(0.5, 0.55), (1.0, 0.5), (1.1892, 0.22), (1.4983, 0.28), (2.0, 0.16)]:
        v = voice(root * ratio, amp, rng.uniform(0.03, 0.09))
        pan = rng.uniform(0.3, 0.7)
        left += v * (1 - pan)
        right += v * pan

    # Uneasy shimmer: a faint high tone a half step above the octave, drifting in and out.
    shimmer = 0.05 * np.sin(2 * np.pi * root * 4 * 1.0595 * t + 3 * np.sin(2 * np.pi * 0.2 * t))
    shimmer *= np.clip(np.sin(2 * np.pi * rng.uniform(0.02, 0.04) * t), 0, None) ** 2
    left += shimmer
    right += shimmer * 0.7

    # Dark wind: brown noise, low-passed, slowly swelling.
    brown = np.cumsum(rng.standard_normal(n))
    brown -= _lowpass(brown, 3.0)  # remove slow drift
    brown /= np.max(np.abs(brown)) + 1e-9
    wind = _lowpass(brown, 400.0) * 0.25 * _lfo(t, 0.05, 0.0, 0.8)
    left += wind
    right += np.roll(wind, SR // 7)

    # Distant low boom roughly every 12-18 seconds.
    pos = rng.uniform(4, 8)
    while pos < seconds - 3:
        i = int(pos * SR)
        length = int(2.5 * SR)
        k = np.arange(min(length, n - i)) / SR
        boom = 0.6 * np.sin(2 * np.pi * (root / 2) * k * (1 - 0.15 * k)) * np.exp(-k * 1.6)
        left[i:i + len(k)] += boom
        right[i:i + len(k)] += boom
        pos += rng.uniform(12, 18)

    left = _reverb(left, rng)
    right = _reverb(right, rng)
    stereo = np.stack([left, right], axis=1)[: int(seconds * SR)]

    # Fade in/out and normalise.
    fade_in, fade_out = int(2.5 * SR), int(3.0 * SR)
    stereo[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
    stereo[-fade_out:] *= np.linspace(1, 0, fade_out)[:, None]
    stereo /= np.max(np.abs(stereo)) + 1e-9
    stereo *= 0.8

    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((stereo * 32767).astype("<i2").tobytes())
    return path


if __name__ == "__main__":
    import sys
    make_music(float(sys.argv[1]) if len(sys.argv) > 1 else 30, sys.argv[2] if len(sys.argv) > 2 else "music.wav",
               int(sys.argv[3]) if len(sys.argv) > 3 else 1)
