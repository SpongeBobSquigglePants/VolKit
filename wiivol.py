"""Extract and convert SpongeBob (WayForward, Wii) DVOL archives and their contents.

Usage:  python wiivol.py [file.vol | folder ...] [-o output_dir]
Needs:  Python 3.8+ and Pillow (pip install pillow)

Folders are searched for .vol files, including subfolders; with no arguments the current folder is used.
Archives a previous run finished are skipped, and only files that failed last time are retried
(progress is kept in _wiivol_status.json in each output folder); use -f to redo everything.
Each archive goes to <name>_extracted/ next to it (or <output_dir>/<name>/ with -o), containing
raw/ (files exactly as stored) and converted/ (PNG / WAV / CSV / GIF versions where the format is known).

Formats (all big-endian):
  DVOL  archive:   'DVOL', u32 version, u32 log2(hash buckets), u32 bucket table offset,
                   u32 file size, u32 entry count, u32 entry table offset.
                   Entry (20 bytes): u32 name hash, u32 name offset, u32 data offset, u32 0, u32 size.
  WIMG  .image:    'WIMG', u8 version, u8 GX format, u8 flags (0xFF = no alpha layer), u8 0,
                   u16 width, height, half width, half height, u32 color layer offset, u32 alpha layer offset.
                   A layer is: u32 header size (incl. itself), RLE bytes over GX tiles in raster order
                   (b & 0x80: b & 0x7F literal tiles follow; else the next tile repeats b times), then tiles.
                   The alpha layer is I4.
  WTEX  .texture:  'WTEX', u8 version, u8 GX format, u8 ?, u8 mip count, u16 width, height; tiles at 0x20.
  WANM  .anim:     'WANM', ..., u16 frame count @0x0E, u32 frame table @0x14, u8 GX format @0x18;
                   u32 sequence count @0x2C, u32 sequence offset @0x30, name @0x34.
                   Frame: u16 width, height, u32 offset (an RLE layer as in WIMG).
                   Sequence step: u16 frame, s16 pivot x, s16 pivot y.
  TXTS  .text:     u16 language count @0x0A, u32 string count @0x14, u32 entry table @0x18.
                   Entry: u32 id hash + one u32 string offset per language (UTF-8, NUL-terminated).
  FONT  .font:     u16 glyph count @0x08, u32 glyph table @0x18, u32 outline table @0x2C.
                   Glyph (20 bytes): u32 code point, u16 w, h, s16 x, y, s16 ?, ?, u32 bitmap offset.
                   Outline (12 bytes): u16 w, h, s16 x, y, u32 bitmap offset. Bitmaps are linear 4bpp.
                   Fonts without an outline have the same value in @0x28 and @0x2C (no outline table).
  WWAV  .wave:     u32 sample rate (float) @0x0C, u32 samples @0x10, u32 loop start/end @0x14/0x18,
                   u16 channels @0x1C, u32 data offset @0x20, u32 channel stride @0x24,
                   u32 channel info offset @0x28 (46 bytes each: Nintendo DSP-ADPCM coefficients etc.).
  Also handled: Nintendo TPL textures and U8 (.arc) archives.
"""
import csv, io, os, struct, sys
from PIL import Image

LANGS = ['English', 'French', 'German', 'Italian', 'Dutch', 'Spanish']

# ---------------------------------------------------------------- GX textures

TILE = {0: (8, 8, 32), 1: (8, 4, 32), 2: (8, 4, 32), 3: (4, 4, 32), 4: (4, 4, 32), 5: (4, 4, 32),
        6: (4, 4, 64), 8: (8, 8, 32), 9: (8, 4, 32), 10: (4, 4, 32), 14: (8, 8, 32)}

def rgb565(c):
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2), 255)

def rgb5a3(c):
    if c & 0x8000:
        r, g, b = (c >> 10) & 31, (c >> 5) & 31, c & 31
        return ((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2), 255)
    a, r, g, b = (c >> 12) & 7, (c >> 8) & 15, (c >> 4) & 15, c & 15
    return (r * 17, g * 17, b * 17, (a << 5) | (a << 2) | (a >> 1))

def ia8(c):
    i = c & 0xFF
    return (i, i, i, c >> 8)

PALETTE_FMT = {0: ia8, 1: rgb565, 2: rgb5a3}

def decode_tile(fmt, t, pal=None):
    """Return a tile as a list of rows of RGBA tuples."""
    tw, th, _ = TILE[fmt]
    if fmt == 14:
        px = [[None] * 8 for _ in range(8)]
        for bi in range(4):
            c0, c1, idx = struct.unpack('>HHI', t[bi * 8:bi * 8 + 8])
            a, b = rgb565(c0), rgb565(c1)
            if c0 > c1:
                p = [a, b, tuple((2 * x + y) // 3 for x, y in zip(a, b)), tuple((x + 2 * y) // 3 for x, y in zip(a, b))]
            else:
                p = [a, b, tuple((x + y) // 2 for x, y in zip(a, b)), (0, 0, 0, 0)]
            bx, by = (bi & 1) * 4, (bi >> 1) * 4
            for i in range(16):
                px[by + i // 4][bx + i % 4] = p[(idx >> (30 - 2 * i)) & 3]
        return px
    if fmt == 6:
        return [[(t[(y * 4 + x) * 2 + 1], t[32 + (y * 4 + x) * 2], t[33 + (y * 4 + x) * 2], t[(y * 4 + x) * 2])
                 for x in range(4)] for y in range(4)]
    vals = []
    if fmt in (0, 8):
        for b in t:
            vals += [b >> 4, b & 15]
    elif fmt in (1, 2, 9):
        vals = list(t)
    else:
        vals = list(struct.unpack('>16H', t))
    if fmt == 0:
        px = [(v * 17, v * 17, v * 17, v * 17) for v in vals]
    elif fmt == 1:
        px = [(v, v, v, v) for v in vals]
    elif fmt == 2:
        px = [((v & 15) * 17,) * 3 + ((v >> 4) * 17,) for v in vals]
    elif fmt == 3:
        px = [ia8(v) for v in vals]
    elif fmt == 4:
        px = [rgb565(v) for v in vals]
    elif fmt == 5:
        px = [rgb5a3(v) for v in vals]
    else:  # C4 / C8 / C14X2
        px = [pal[v & 0x3FFF] if pal and (v & 0x3FFF) < len(pal) else (0, 0, 0, 0) for v in vals]
    return [px[y * tw:(y + 1) * tw] for y in range(th)]

def tiles_to_image(fmt, tiles, w, h, pal=None):
    tw, th, _ = TILE[fmt]
    cols = (w + tw - 1) // tw
    img = Image.new('RGBA', (cols * tw, ((h + th - 1) // th) * th))
    pix = img.load()
    for n, t in enumerate(tiles):
        ox, oy = (n % cols) * tw, (n // cols) * th
        for y, row in enumerate(decode_tile(fmt, t, pal)):
            for x, p in enumerate(row):
                pix[ox + x, oy + y] = p
    return img.crop((0, 0, w, h))

def linear_tiles(fmt, d, off, w, h):
    tw, th, bs = TILE[fmt]
    n = ((w + tw - 1) // tw) * ((h + th - 1) // th)
    return [d[off + i * bs:off + (i + 1) * bs] for i in range(n)]

def rle_tiles(fmt, d, off, w, h):
    tw, th, bs = TILE[fmt]
    n = ((w + tw - 1) // tw) * ((h + th - 1) // th)
    hsz = struct.unpack('>I', d[off:off + 4])[0]
    p, tiles = off + hsz, []
    for b in d[off + 4:off + hsz]:
        if len(tiles) >= n:
            break
        if b & 0x80:
            for _ in range(b & 0x7F):
                tiles.append(d[p:p + bs]); p += bs
        elif b:
            tiles.extend([d[p:p + bs]] * b); p += bs
    return tiles

def wimg(d):
    fmt, flags = d[5], d[6]
    w, h = struct.unpack('>HH', d[8:12])
    o1, o2 = struct.unpack('>II', d[16:24])
    img = tiles_to_image(fmt, rle_tiles(fmt, d, o1, w, h), w, h)
    if flags != 0xFF:
        img.putalpha(tiles_to_image(0, rle_tiles(0, d, o2, w, h), w, h).getchannel('A'))
    return img

def wtex(d):
    w, h = struct.unpack('>HH', d[8:12])
    return tiles_to_image(d[5], linear_tiles(d[5], d, 0x20, w, h), w, h)

def read_palette(d, off, count, pfmt):
    return [PALETTE_FMT[pfmt](v) for v in struct.unpack('>%dH' % count, d[off:off + count * 2])]

def tpl(d):
    n, tab = struct.unpack('>II', d[4:12])
    out = []
    for i in range(n):
        ih, ph = struct.unpack('>II', d[tab + i * 8:tab + i * 8 + 8])
        h, w, fmt, off = struct.unpack('>HHII', d[ih:ih + 12])
        pal = None
        if ph:
            cnt, _, _, pfmt, poff = struct.unpack('>HBBII', d[ph:ph + 12])
            pal = read_palette(d, poff, cnt, pfmt)
        out.append(tiles_to_image(fmt, linear_tiles(fmt, d, off, w, h), w, h, pal))
    return out

# ---------------------------------------------------------------- animations

def wanm(d):
    """Return (frames, sequence) where sequence is a list of (frame index, pivot x, pivot y)."""
    nframes, = struct.unpack('>H', d[0x0E:0x10])
    ftab, = struct.unpack('>I', d[0x14:0x18])
    fmt = d[0x18]
    frames, errors = [], []
    for i in range(nframes):
        try:
            w, h, off = struct.unpack('>HHI', d[ftab + i * 8:ftab + i * 8 + 8])
            frames.append(tiles_to_image(fmt, rle_tiles(fmt, d, off, w, h), w, h))
        except Exception as e:
            frames.append(None)
            errors.append('frame %d: %s' % (i, describe(e)))
    seqs = wanm_sequences(d)
    seq = [s for s in seqs[0][1] if s[0] < len(frames) and frames[s[0]] is not None]
    return frames, seq, errors

def wanm_sequences(d):
    """Every playback sequence in a WANM file as (name, [(frame, pivot x, pivot y)]). Most files have one.
    u32 sequence count @0x1C, then one 20-byte record per sequence from 0x20: name offset, name hash,
    f32 speed (always 1.0), u32 step count, u32 step table offset."""
    n, = struct.unpack('>I', d[0x1C:0x20])
    out = []
    for i in range(n):
        noff, _, _, nsteps, soff = struct.unpack('>IIfII', d[0x20 + i * 20:0x34 + i * 20])
        name = d[noff:d.index(b'\0', noff)].decode('latin-1')
        out.append((name, [struct.unpack('>Hhh', d[soff + j * 6:soff + j * 6 + 6]) for j in range(nsteps)]))
    return out

def anim_gif(frames, seq, path, ms=66):
    left = max(px for _, px, _ in seq); top = max(py for _, _, py in seq)
    right = max(frames[f].width - px for f, px, _ in seq); bottom = max(frames[f].height - py for f, _, py in seq)
    out = []
    for f, px, py in seq:
        c = Image.new('RGBA', (left + right, top + bottom), (0, 0, 0, 0))
        c.alpha_composite(frames[f], (left - px, top - py))
        out.append(c)
    out[0].save(path, save_all=True, append_images=out[1:], duration=ms, loop=0, disposal=2)

# ---------------------------------------------------------------- text / fonts / audio

def txts(d):
    nlang, = struct.unpack('>H', d[0x0A:0x0C])
    n, tab = struct.unpack('>II', d[0x14:0x1C])
    rows = []
    for i in range(n):
        e = struct.unpack('>%dI' % (nlang + 1), d[tab + i * (4 + 4 * nlang):tab + (i + 1) * (4 + 4 * nlang)])
        rows.append(['%08X' % e[0]] + [d[o:d.index(b'\0', o)].decode('utf-8', 'replace') for o in e[1:]])
    langs = LANGS if nlang == len(LANGS) else ['Language%d' % i for i in range(nlang)]
    return ['ID hash'] + langs, rows

def font(d):
    """Render every glyph (white fill over black outline, if the font has one) onto one sheet;
    return sheet and metrics rows."""
    n, = struct.unpack('>H', d[0x08:0x0A])
    gtab, = struct.unpack('>I', d[0x18:0x1C])
    osec, otab = struct.unpack('>II', d[0x28:0x30])
    has_outline = otab != osec

    def bitmap(w, h, off, color):
        stride = (w + 1) // 2
        if w > 2048 or h > 2048 or off + stride * h > len(d):
            raise ValueError('bad glyph bitmap %dx%d at 0x%X' % (w, h, off))
        im = Image.new('RGBA', (max(w, 1), max(h, 1)), color + (0,))
        if w and h:
            a = bytearray()
            for y in range(h):
                for b in d[off + y * stride:off + (y + 1) * stride]:
                    a += bytes(((b >> 4) * 17, (b & 15) * 17))
                del a[len(a) - (stride * 2 - w):]
            im.putalpha(Image.frombytes('L', (w, h), bytes(a)))
        return im

    glyphs, rows = [], []
    empty = Image.new('RGBA', (1, 1))
    for i in range(n):
        cp, w, h, x, y, u1, u2, off = struct.unpack('>IHHhhhhI', d[gtab + i * 20:gtab + i * 20 + 20])
        if has_outline:
            ow, oh, ox, oy, ooff = struct.unpack('>HHhhI', d[otab + i * 12:otab + i * 12 + 12])
            outline = bitmap(ow, oh, ooff, (0, 0, 0))
        else:
            outline, ox, oy = empty, x, y
        glyphs.append((cp, bitmap(w, h, off, (255, 255, 255)), x, y, outline, ox, oy))
        rows.append([cp, chr(cp) if cp > 32 else '', w, h, x, y, u1, u2])
    glyphs.sort(key=lambda g: g[0]); rows.sort(key=lambda r: r[0])
    cell = max(max(g[1].width, g[4].width, g[1].height, g[4].height) for g in glyphs) + 8
    cols = 16
    sheet = Image.new('RGBA', (cols * cell, ((n + cols - 1) // cols) * cell), (90, 140, 200, 255))
    for i, (cp, fill, x, y, outl, ox, oy) in enumerate(glyphs):
        bx, by = (i % cols) * cell + 4, (i // cols) * cell + 4
        sheet.alpha_composite(outl, (bx + max(ox - min(x, ox), 0), by + max(oy - min(y, oy), 0)))
        sheet.alpha_composite(fill, (bx + max(x - min(x, ox), 0), by + max(y - min(y, oy), 0)))
    return sheet, rows

def dsp_decode(d, off, nsamples, coefs, hist1=0, hist2=0):
    out = bytearray()
    pos = off
    left = nsamples
    while left > 0:
        ps = d[pos]; pos += 1
        scale, c1, c2 = 1 << (ps & 15), coefs[(ps >> 4) * 2], coefs[(ps >> 4) * 2 + 1]
        for i in range(min(14, left)):
            b = d[pos + i // 2]
            n = (b >> 4) if i % 2 == 0 else (b & 15)
            if n >= 8:
                n -= 16
            s = ((n * scale) << 11) + 1024 + c1 * hist1 + c2 * hist2
            s = max(-32768, min(32767, s >> 11))
            out += struct.pack('<h', s)
            hist2, hist1 = hist1, s
        pos += 7
        left -= 14
    return out

def wwav(d):
    rate, = struct.unpack('>f', d[0x0C:0x10])
    nsamples, = struct.unpack('>I', d[0x10:0x14])
    ch, = struct.unpack('>H', d[0x1C:0x1E])
    doff, stride, info = struct.unpack('>III', d[0x20:0x2C])
    chans = []
    for c in range(ch):
        base = info + c * 46
        coefs = struct.unpack('>16h', d[base:base + 32])
        h1, h2 = struct.unpack('>hh', d[base + 36:base + 40])
        chans.append(dsp_decode(d, doff + c * stride, nsamples, coefs, h1, h2))
    pcm = bytearray()
    if ch == 1:
        pcm = chans[0]
    else:
        for i in range(0, len(chans[0]), 2):
            for c in chans:
                pcm += c[i:i + 2]
    buf = io.BytesIO()
    import wave
    with wave.open(buf, 'wb') as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(int(rate)); w.writeframes(bytes(pcm))
    return buf.getvalue()

# ---------------------------------------------------------------- archives

def u8(d):
    """Yield (path, bytes) for every file in a Nintendo U8 archive."""
    root, hsize, doff = struct.unpack('>III', d[4:16])
    total = struct.unpack('>I', d[root + 8:root + 12])[0]
    strs = root + total * 12
    def name(o):
        return d[strs + o:d.index(b'\0', strs + o)].decode('latin-1')
    stack, i = [('', total)], 1
    while i < total:
        while stack and i >= stack[-1][1]:
            stack.pop()
        typ = d[root + i * 12]
        noff = struct.unpack('>I', b'\0' + d[root + i * 12 + 1:root + i * 12 + 4])[0]
        a, b = struct.unpack('>II', d[root + i * 12 + 4:root + i * 12 + 12])
        path = '/'.join([s[0] for s in stack if s[0]] + [name(noff)])
        if typ == 1:
            stack.append((name(noff) if i else '', b))
        else:
            yield path, d[a:a + b]
        i += 1

def dvol(d):
    assert d[:4] == b'DVOL', 'not a DVOL archive'
    n, tab = struct.unpack('>II', d[0x14:0x1C])
    for i in range(n):
        _, no, off, _, sz = struct.unpack('>5I', d[tab + i * 20:tab + i * 20 + 20])
        yield d[no:d.index(b'\0', no)].decode('latin-1').replace('\\', '/'), d[off:off + sz]

# ---------------------------------------------------------------- driver

def describe(e):
    """Error text plus the wiivol.py line it came from, to make bug reports useful."""
    import traceback
    lines = [f for f in traceback.extract_tb(e.__traceback__) if f.filename.endswith(('wiivol.py', 'volpack.py'))]
    where = ' (line %d, %s)' % (lines[-1].lineno, lines[-1].name) if lines else ''
    return '%s: %s%s' % (type(e).__name__, e, where)

def safe_join(base, name):
    parts = [p for p in name.replace('\\', '/').split('/') if p not in ('', '.', '..')]
    return os.path.join(base, *parts)

def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)

def convert(name, d, out, log):
    """Write converted versions of one file under out; recurse into U8 archives."""
    stem, ext = os.path.splitext(safe_join(out, name))
    ext = ext.lower()
    try:
        if d[:4] == b'WIMG':
            os.makedirs(os.path.dirname(stem), exist_ok=True); wimg(d).save(stem + '.png')
        elif d[:4] == b'WTEX':
            os.makedirs(os.path.dirname(stem), exist_ok=True); wtex(d).save(stem + '.png')
        elif d[:4] == b'\x00\x20\xaf\x30':
            os.makedirs(os.path.dirname(stem), exist_ok=True)
            for i, im in enumerate(tpl(d)):
                im.save(stem + ('' if i == 0 else '_%d' % i) + '.png')
        elif d[:4] == b'WANM':
            frames, seq, errors = wanm(d)
            os.makedirs(stem, exist_ok=True)
            for i, im in enumerate(frames):
                if im is not None:
                    im.save(os.path.join(stem, 'frame_%02d.png' % i))
            if seq:
                try:
                    anim_gif(frames, seq, stem + '.gif')
                except Exception as e:
                    errors.append('GIF: %s' % describe(e))
            for err in errors:
                log.append('PARTLY FAILED %s (%d frames saved) %s' % (name, sum(f is not None for f in frames), err))
            if errors:
                return not all(f is None for f in frames)
        elif d[:4] == b'TXTS':
            head, rows = txts(d)
            os.makedirs(os.path.dirname(stem), exist_ok=True)
            with open(stem + '.csv', 'w', newline='', encoding='utf-8-sig') as f:
                csv.writer(f).writerows([head] + rows)
        elif d[:4] == b'FONT':
            sheet, rows = font(d)
            os.makedirs(os.path.dirname(stem), exist_ok=True)
            sheet.save(stem + '_glyphs.png')
            with open(stem + '_metrics.csv', 'w', newline='', encoding='utf-8-sig') as f:
                csv.writer(f).writerows([['code point', 'char', 'width', 'height', 'x', 'y', 'unk1', 'unk2']] + rows)
        elif d[:4] == b'WWAV':
            write(stem + '.wav', wwav(d))
        elif d[:4] == b'U\xaa8-':
            for sub, sd in u8(d):
                write(safe_join(stem + ext.replace('.', '_'), sub), sd)
                convert(os.path.join(os.path.relpath(stem, out) + ext.replace('.', '_'), sub), sd, out, log)
        elif ext in ('.cfg', '.txt', '.csv', '') and d[:2] in (b'\xff\xfe', b'\xfe\xff'):
            write(stem + ext, d.decode('utf-16').encode('utf-8-sig'))
        else:
            return False
        return True
    except Exception as e:
        log.append('FAILED %s: %s' % (name, describe(e)))
        return False

OUT_SUFFIX = '_extracted'

def find_vols(paths):
    """Expand folders (searched recursively, skipping our own output folders) into .vol files."""
    vols = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(x for x in dirs if not x.endswith(OUT_SUFFIX))
                vols += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith('.vol')]
        elif os.path.isfile(p):
            vols.append(p)
        else:
            print('Not found: %s' % p)
    return vols

STATUS_FILE = '_wiivol_status.json'

def read_status(base, vol):
    """Return the saved status if it belongs to this exact .vol (same size and date), else None."""
    import json
    try:
        with open(os.path.join(base, STATUS_FILE), encoding='utf-8') as f:
            st = json.load(f)
        info = os.stat(vol)
        if st['vol_size'] == info.st_size and st['vol_mtime'] == int(info.st_mtime):
            return st
    except (OSError, ValueError, KeyError):
        pass
    return None

def write_status(base, vol, failed):
    import json
    info = os.stat(vol)
    with open(os.path.join(base, STATUS_FILE), 'w', encoding='utf-8') as f:
        json.dump({'vol_size': info.st_size, 'vol_mtime': int(info.st_mtime), 'vol_path': os.path.abspath(vol),
                   'failed': sorted(failed)}, f, indent=1)

def read_status_any(base):
    """The saved status of an output folder, whatever .vol it came from (volpack.py uses it to find the .vol)."""
    import json
    try:
        with open(os.path.join(base, STATUS_FILE), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def extract_vol(vol, base, force=False):
    """Extract one .vol. Skips it if a previous run finished it, and only retries files that failed last time."""
    st = None if force else read_status(base, vol)
    if st is not None and not st['failed']:
        print('%s: already extracted, skipped' % vol)
        return
    with open(vol, 'rb') as f:
        d = f.read()
    if d[:4] != b'DVOL':
        print('%s: skipped (not a DVOL archive)' % vol)
        return
    retry = set(st['failed']) if st is not None else None
    if retry:
        print('%s: retrying %d file(s) that failed last time' % (vol, len(retry)))
    log, failed, ok, total = [], set(), 0, 0
    for name, data in dvol(d):
        if retry is not None and name not in retry:
            continue
        total += 1
        write(safe_join(os.path.join(base, 'raw'), name), data)
        before = len(log)
        ok += convert(name, data, os.path.join(base, 'converted'), log)
        if len(log) > before:
            failed.add(name)
    write_status(base, vol, failed)
    print('%s: %d files, %d converted -> %s' % (vol, total, ok, base))
    for line in log:
        print('  ' + line)

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='Extract and convert DVOL (.vol) archives.')
    ap.add_argument('paths', nargs='*', default=['.'],
                    help='.vol files or folders to search (default: current folder)')
    ap.add_argument('-o', '--out', help='put all output here instead of next to each .vol')
    ap.add_argument('-f', '--force', action='store_true',
                    help='extract everything again, even archives a previous run already finished')
    args = ap.parse_args(argv[1:])
    vols = find_vols(args.paths)
    if not vols:
        print('No .vol files found in: %s' % ', '.join(os.path.abspath(p) for p in args.paths))
        return
    print('Found %d .vol file(s).' % len(vols))
    for vol in vols:
        stem = os.path.splitext(os.path.basename(vol))[0]
        base = os.path.join(args.out, stem) if args.out else os.path.join(os.path.dirname(vol), stem + OUT_SUFFIX)
        try:
            extract_vol(vol, base, args.force)
        except Exception as e:
            print('%s: FAILED (%s)' % (vol, e))

if __name__ == '__main__':
    main(sys.argv)
