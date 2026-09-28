"""Deterministic, text-free UI backdrop using the website's blue/white palette."""
from pathlib import Path
from PIL import Image
import numpy as np

W, H = 880, 600
logo = Image.open(Path(__file__).resolve().parents[2] / 'client/webui/static/logo.png').convert('RGBA')
logo.thumbnail((76, 76), Image.Resampling.LANCZOS)
out = Path(__file__).with_name('installer-wave')
out.mkdir(exist_ok=True)
y, x = np.mgrid[0:H, 0:W]
diagonal = .55*x/W + .45*y/H
for frame in range(80):
    phase = frame / 80 * 2*np.pi
    blend = (np.sin(diagonal*2*np.pi-phase)+1)/2
    rgb = np.array((196,220,255)) + blend[...,None]*np.array((44,25,0))
    im = Image.fromarray(rgb.astype('uint8'))
    im.paste(logo, ((W-logo.width)//2, 122), logo)
    im.save(out / f'wave-{frame}.bmp')
    if frame == 0:
        im.save(Path(__file__).with_name('installer-backdrop.bmp'))
