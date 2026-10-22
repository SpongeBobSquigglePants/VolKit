"""VolPacker: put edited files back into SpongeBob SquigglePants (Wii) DVOL archives.

Usage:  python volpack.py <name>_extracted [...] [--vol original.vol] [-o new.vol] [--all]
Needs:  Python 3.8+ and Pillow (pip install pillow); wiivol.py next to this file

Takes a folder VolExtractor (wiivol.py) made, finds what was changed in it and writes a new .vol:
  converted/  edited PNGs, CSVs, WAVs and text files are converted back into the game's formats
  raw/        changed files are used as they are, and files that weren't in the archive are added
Every file that wasn't changed keeps its exact bytes, so packing an unedited folder gives back the original .vol.
The original .vol is found next to the folder (<name>.vol for <name>_extracted), or given with --vol.
Without -o the new archive is written next to the folder as <name>_new.vol; the original is never overwritten
unless -o names it.

What converts back (the rest can be replaced through raw/):
  .image   (WIMG)  from a PNG, any size, in the original's GX format (CMPR, RGBA8, RGB565, RGB5A3 or I4)
  .texture (WTEX)  from a PNG, with its mipmaps made again
  .anim    (WANM)  from the frame_NN.png files (the GIF is only a preview); timing and pivots are kept
  .text    (TXTS)  from the CSV: changed strings, in any language
  .wave    (WWAV)  from a WAV (16-bit PCM, mono or stereo, any sample rate), encoded as Nintendo DSP-ADPCM
  UTF-16 .cfg and .txt, from the UTF-8 text file
Fonts, .tpl textures and U8 (.arc) archives don't convert back yet: replace them in raw/.

Formats (all big-endian) are described at the top of wiivol.py; the layouts written here match the game's own:
  WIMG: colour layer at 0x20, the I4 alpha layer at the next multiple of 32.  WTEX: tiles of each mip level in
  turn from 0x20.  WANM: each frame's layer starts at a multiple of 32.  WWAV: channel info at 0x2C, 46 bytes a
  channel; data at the next multiple of 32 after it plus 32; each channel padded to 32, plus 64 between channels.
  DVOL: entries kept in hash groups by the low bits of the lookup2 hash of the lowercased name.
"""
import csv
import io
import os
import struct
import sys
import wave

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wiivol  # noqa: E402

def align(x, a=32):
    return (x + a - 1) // a * a

# ---------------------------------------------------------------- DVOL archives

def name_hash(name):
    """The game's file name hash: Bob Jenkins' lookup2 (initval 0) over the lowercased name."""
    M = 0xFFFFFFFF

    def mix(a, b, c):
        a = (a - b - c) & M; a ^= c >> 13
        b = (b - c - a) & M; b ^= (a << 8) & M
        c = (c - a - b) & M; c ^= b >> 13
        a = (a - b - c) & M; a ^= c >> 12
        b = (b - c - a) & M; b ^= (a << 16) & M
        c = (c - a - b) & M; c ^= b >> 5
        a = (a - b - c) & M; a ^= c >> 3
        b = (b - c - a) & M; b ^= (a << 10) & M
        c = (c - a - b) & M; c ^= b >> 15
        return a, b, c
    k = name.lower().encode('latin-1')
    a = b = 0x9E3779B9
    c, i, n = 0, 0, len(k)
    while n - i >= 12:
        a = (a + int.from_bytes(k[i:i + 4], 'little')) & M
        b = (b + int.from_bytes(k[i + 4:i + 8], 'little')) & M
        c = (c + int.from_bytes(k[i + 8:i + 12], 'little')) & M
        a, b, c = mix(a, b, c)
        i += 12
    c = (c + n) & M
    for j, ch in enumerate(k[i:]):
        if j < 4:
            a = (a + (ch << (8 * j))) & M
        elif j < 8:
            b = (b + (ch << (8 * (j - 4)))) & M
        else:
            c = (c + (ch << (8 * (j - 7)))) & M
    return mix(a, b, c)[2]


class Vol:
    """A DVOL archive, to change and write out again. Entries keep their order, hash groups and names."""

    def __init__(self, data):
        assert data[:4] == b'DVOL', 'not a DVOL archive'
        self.orig = bytes(data)
        n, self.tab = struct.unpack('>II', data[0x14:0x1C])
        self.entries = []
        for i in range(n):
            h, no, off, flag, size = struct.unpack('>5I', data[self.tab + i * 20:self.tab + i * 20 + 20])
            name = data[no:data.index(b'\0', no)].decode('latin-1')
            self.entries.append({'hash': h, 'name': name, 'flag': flag, 'off': off, 'noff': no,
                                 'data': data[off:off + size], 'orig': data[off:off + size]})
        self.added = []

    @classmethod
    def load(cls, path):
        with open(path, 'rb') as f:
            return cls(f.read())

    def names(self):
        return [e['name'] for e in self.entries] + [e['name'] for e in self.added]

    def find(self, name):
        key = name.replace('\\', '/').lower()
        for e in self.entries + self.added:
            if e['name'].replace('\\', '/').lower() == key:
                return e
        return None

    def get(self, name):
        return self.find(name)['data']

    def changed(self, name):
        e = self.find(name)
        return e in self.added or e['data'] != e['orig']

    def replace(self, name, data):
        e = self.find(name)
        if e is None:
            raise KeyError(name)
        e['data'] = bytes(data)

    def revert(self, name):
        e = self.find(name)
        if e in self.added:
            self.added.remove(e)
        else:
            e['data'] = e['orig']

    def add(self, name, data):
        if self.find(name) is not None:
            raise KeyError('%s is already in the archive' % name)
        self.added.append({'hash': name_hash(name), 'name': name, 'flag': 0, 'data': bytes(data)})

    def build(self):
        """The archive's bytes. With nothing added, the layout (header, entry order, 32-byte aligned data in
        entry order, then the names) is exactly the original's, so an unchanged archive comes back identical."""
        d = self.orig
        n = len(self.entries)
        ents = list(self.entries)
        head = bytearray(d[:self.tab])
        if self.added:
            slots = (self.tab - 0x1E) // 2
            allends = list(struct.unpack('>%dH' % slots, d[0x1E:self.tab]))
            groups = next(g for g in (8, 16, 32, 64, 128, 256) if g <= slots and allends[g - 1] == n)
            ends = allends[:groups]
            for e in self.added:
                g = e['hash'] % groups
                ents.insert(ends[g], e)
                for k in range(g, groups):
                    ends[k] += 1
            struct.pack_into('>I', head, 0x14, len(ents))
            struct.pack_into('>%dH' % groups, head, 0x1E, *ends)
        out = head + b'\0' * (20 * len(ents))
        first = min(e['off'] for e in self.entries) if self.entries else len(out)
        if not self.added and len(out) <= first:
            out = bytearray(d[:first])  # keeps any bytes between the entry table and the first file
        for e in sorted(ents, key=lambda e: e.get('off', 1 << 40)):  # files in their original order, new ones last
            while len(out) % 32:
                out.append(0)
            e['newoff'] = len(out)
            out += e['data']
        base = len(out)
        if self.added:
            names = bytearray()
            for e in sorted(self.entries, key=lambda e: e['noff']) + self.added:
                e['nrel'] = len(names)
                names += e['name'].encode('latin-1') + b'\0'
            out += names
        else:
            nstart = min(e['noff'] for e in self.entries)
            out += d[nstart:]
            for e in self.entries:
                e['nrel'] = e['noff'] - nstart
        for i, e in enumerate(ents):
            struct.pack_into('>5I', out, self.tab + i * 20, e['hash'], base + e['nrel'], e['newoff'], e['flag'],
                             len(e['data']))
        struct.pack_into('>I', out, 0x10, len(out))
        return bytes(out)

    def save(self, path):
        data = self.build()
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)

# ---------------------------------------------------------------- GX tile encoders

def _565(p):
    return ((p[0] >> 3) << 11) | ((p[1] >> 2) << 5) | (p[2] >> 3)

def _5a3(p):
    r, g, b, a = p
    opaque = 0x8000 | ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)
    clear = ((a >> 5) << 12) | ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
    if a == 255:  # the 4-bit form with alpha 7 is opaque too: use it when it's exact (a round trip of a game file)
        return clear if wiivol.rgb5a3(clear) == p and wiivol.rgb5a3(opaque) != p else opaque
    return opaque if a >= 0xF0 else clear

def _cmpr_block(blk):
    """One 4x4 DXT1 block (16 RGBA tuples) in 4-colour mode: endpoints at the ends of the colours' main axis."""
    pts = [p[:3] for p in blk if p[3] >= 128] or [p[:3] for p in blk]
    n = len(pts)
    mean = [sum(p[i] for p in pts) / n for i in range(3)]
    cov = [[sum((p[i] - mean[i]) * (p[j] - mean[j]) for p in pts) for j in range(3)] for i in range(3)]
    v = [1.0, 1.0, 1.0]
    for _ in range(4):  # power iteration: the axis the colours spread along
        v = [sum(cov[i][j] * v[j] for j in range(3)) for i in range(3)]
        m = max(abs(x) for x in v) or 1.0
        v = [x / m for x in v]
    proj = [sum((p[i] - mean[i]) * v[i] for i in range(3)) for p in pts]
    lo, hi = pts[proj.index(min(proj))], pts[proj.index(max(proj))]
    c0, c1 = _565(hi), _565(lo)
    if c0 < c1:
        c0, c1 = c1, c0
    pal = [wiivol.rgb565(c0)[:3], wiivol.rgb565(c1)[:3]]
    if c0 > c1:
        pal += [tuple((2 * x + y) // 3 for x, y in zip(pal[0], pal[1])),
                tuple((x + 2 * y) // 3 for x, y in zip(pal[0], pal[1]))]
    idx = 0
    for i, p in enumerate(blk):
        k = min(range(len(pal)), key=lambda j: (p[0] - pal[j][0]) ** 2 + (p[1] - pal[j][1]) ** 2 + (p[2] - pal[j][2]) ** 2)
        idx |= k << (30 - 2 * i)
    return struct.pack('>HHI', c0, c1, idx)

def encode_tile(fmt, rows):
    """rows: the tile's pixels (TILE[fmt] in size) as rows of RGBA tuples."""
    if fmt == 14:
        out = b''
        for bi in range(4):
            bx, by = (bi & 1) * 4, (bi >> 1) * 4
            out += _cmpr_block([rows[by + y][bx + x] for y in range(4) for x in range(4)])
        return out
    px = [p for r in rows for p in r]
    if fmt == 6:
        return bytes(b for p in px for b in (p[3], p[0])) + bytes(b for p in px for b in (p[1], p[2]))
    if fmt == 5:
        return struct.pack('>16H', *[_5a3(p) for p in px])
    if fmt == 4:
        return struct.pack('>16H', *[_565(p) for p in px])
    if fmt in (0, 8):  # I4: brightness (or the alpha layer's alpha, as the game's alpha layers do)
        vals = [(((p[0] * 299 + p[1] * 587 + p[2] * 114) // 1000) + 8) // 17 for p in px]
        return bytes((vals[i] << 4) | vals[i + 1] for i in range(0, len(vals), 2))
    raise ValueError('no encoder for GX format %d' % fmt)

def image_tiles(fmt, img):
    """The image as GX tiles in raster order (edge tiles padded by repeating the last row and column)."""
    tw, th, _ = wiivol.TILE[fmt]
    img = img.convert('RGBA')
    w, h = img.size
    pix = img.load()
    tiles = []
    for ty in range(0, h, th):
        for tx in range(0, w, tw):
            rows = [[pix[min(w - 1, tx + x), min(h - 1, ty + y)] for x in range(tw)] for y in range(th)]
            tiles.append(encode_tile(fmt, rows))
    return tiles

def alpha_tiles(img):
    """The I4 alpha layer: the image's alpha as brightness."""
    a = img.convert('RGBA').getchannel('A')
    return image_tiles(0, Image.merge('RGBA', (a, a, a, a)))

def rle_layer(tiles):
    """A run-coded layer: u32 header size (incl. itself), run bytes (0x80 | n: n stored tiles follow; n: one tile
    repeated n times), padded to 4, then the stored tiles."""
    ctl, data, i = bytearray(), bytearray(), 0
    while i < len(tiles):
        j = i
        while j < len(tiles) and tiles[j] == tiles[i] and j - i < 127:
            j += 1
        if j - i >= 2:
            ctl.append(j - i); data += tiles[i]; i = j
            continue
        j = i
        while j < len(tiles) and j - i < 127 and not (j + 1 < len(tiles) and tiles[j + 1] == tiles[j]):
            j += 1
        j = max(j, i + 1)
        ctl.append(0x80 | (j - i))
        for t in tiles[i:j]:
            data += t
        i = j
    while (4 + len(ctl)) % 4:
        ctl.append(0)
    return struct.pack('>I', 4 + len(ctl)) + bytes(ctl) + bytes(data)

def has_transparency(img):
    return img.mode in ('RGBA', 'LA', 'PA') and img.getchannel('A').getextrema()[0] < 255 or \
        (img.mode == 'P' and 'transparency' in img.info)

# ---------------------------------------------------------------- writers, one per format

def make_wimg(orig, img):
    """A WIMG from a picture, in the original's GX format; the size may change. An alpha layer is written if the
    original had one, or if the new picture has see-through parts and the format can't hold them itself."""
    img = img.convert('RGBA')
    fmt = orig[5]
    w, h = img.size
    ow, oh, ohw, ohh = struct.unpack('>4H', orig[8:16])
    half = lambda new, old, oldhalf: new // 2 if oldhalf == old // 2 else (
        (new + 1) // 2 if oldhalf == (old + 1) // 2 else round(new * oldhalf / max(old, 1)))
    # RGBA8 and RGB5A3 hold their own alpha; the others need the separate alpha layer for see-through parts
    alpha = orig[6] != 0xFF or (fmt not in (5, 6) and has_transparency(img))
    head = bytearray(orig[:0x20])
    struct.pack_into('>4H', head, 8, w, h, half(w, ow, ohw), half(h, oh, ohh))
    colour = rle_layer(image_tiles(fmt, img))
    out = head + colour
    if alpha:
        head[6] = 0 if orig[6] == 0xFF else orig[6]
        a_off = align(len(out))
        out = head + colour + b'\0' * (a_off - len(out)) + rle_layer(alpha_tiles(img))
        struct.pack_into('>II', out, 16, 0x20, a_off)
    else:
        struct.pack_into('>I', out, 16, 0x20)
        while len(out) % 4:
            out.append(0)
    return bytes(out)

def make_wtex(orig, img):
    """A WTEX from a picture, in the original's format, with the same number of mipmaps (each half the last)."""
    fmt, mips = orig[5], orig[7]
    img = img.convert('RGBA')
    head = bytearray(orig[:0x20])
    struct.pack_into('>HH', head, 8, *img.size)
    out = bytearray(head)
    level = img
    for _ in range(max(1, mips)):
        out += b''.join(image_tiles(fmt, level))
        level = level.resize((max(1, level.width // 2), max(1, level.height // 2)), Image.LANCZOS)
    return bytes(out)

def make_wanm(orig, frames):
    """A WANM with new frame pictures (a list, None for a frame to keep). Frames may change size; the frame
    table, sequences, pivots and names are kept."""
    fmt = orig[0x18]
    nframes, = struct.unpack('>H', orig[0x0E:0x10])
    ftab, = struct.unpack('>I', orig[0x14:0x18])
    if len(frames) != nframes:
        raise ValueError('the animation has %d frames, but %d pictures were given' % (nframes, len(frames)))
    table = [struct.unpack('>HHI', orig[ftab + i * 8:ftab + i * 8 + 8]) for i in range(nframes)]
    first = min(off for _, _, off in table)
    out = bytearray(orig[:first])
    done = {}
    for i, (w, h, off) in enumerate(table):
        if frames[i] is None and off in done:
            struct.pack_into('>HHI', out, ftab + i * 8, w, h, done[off])
            continue
        if frames[i] is None:
            layer = rle_layer(wiivol.rle_tiles(fmt, orig, off, w, h))  # the same tiles, run-coded again
        else:
            img = frames[i].convert('RGBA')
            w, h = img.size
            layer = rle_layer(image_tiles(fmt, img))
        while len(out) % 32:
            out.append(0)
        if frames[i] is None:
            done[off] = len(out)
        struct.pack_into('>HHI', out, ftab + i * 8, w, h, len(out))
        out += layer
    return bytes(out)

def _txts_rows(d):
    nlang, = struct.unpack('>H', d[0x0A:0x0C])
    n, tab = struct.unpack('>II', d[0x14:0x1C])
    stride = 4 + 4 * nlang
    return nlang, tab, stride, [list(struct.unpack('>%dI' % (nlang + 1), d[tab + i * stride:tab + (i + 1) * stride]))
                                for i in range(n)]

def _same_text(a, b):
    norm = lambda s: s.replace('\r\n', '\n').replace('\r', '\n')
    return norm(a) == norm(b)

def _line_ends_like(old, new):
    """New text with its line breaks written the way the old text wrote them (spreadsheet programs change them)."""
    new = new.replace('\r\n', '\n').replace('\r', '\n')
    if '\r\n' in old:
        return new.replace('\n', '\r\n')
    if '\n' in old and '\r' not in old:
        return new
    return new.replace('\n', '\r')

def txts_changes(orig, table):
    """Compare a text table (rows of [hash, text per language], as the CSV holds) with the original; returns
    {(row, language): new text} for the strings that are different."""
    nlang, tab, stride, rows = _txts_rows(orig)
    by_hash = {}
    for r in table:
        if r and r[0].strip():
            try:
                by_hash[int(r[0].strip(), 16)] = r[1:]
            except ValueError:
                pass  # the header row
    changes = {}
    for i, e in enumerate(rows):
        new = by_hash.get(e[0])
        if new is None:
            continue
        for lang in range(nlang):
            if lang >= len(new):
                continue
            old = orig[e[1 + lang]:orig.index(b'\0', e[1 + lang])].decode('utf-8', 'replace')
            if not _same_text(old, new[lang]):
                changes[(i, lang)] = _line_ends_like(old, new[lang])
    return changes

def make_txts(orig, changes):
    """A TXTS with some strings changed ({(row, language): text}). The new strings go after the old ones (the
    format has no size field), and their entries point at them; the rest of the file is unchanged."""
    nlang, tab, stride, rows = _txts_rows(orig)
    out = bytearray(orig)
    for (r, lang), text in sorted(changes.items()):
        rows[r][1 + lang] = len(out)
        out += text.encode('utf-8') + b'\0'
    for i, e in enumerate(rows):
        struct.pack_into('>%dI' % (nlang + 1), out, tab + i * stride, *e)
    return bytes(out)

def read_csv(data):
    text = data.decode('utf-8-sig') if isinstance(data, bytes) else data
    return list(csv.reader(io.StringIO(text, newline='')))

def make_text16(orig, data):
    """A UTF-16 .cfg or .txt from UTF-8 text, with the original's byte order mark."""
    text = data.decode('utf-8-sig')
    big = orig[:2] == b'\xfe\xff'
    return (b'\xfe\xff' if big else b'\xff\xfe') + text.encode('utf-16-be' if big else 'utf-16-le')

# ---------------------------------------------------------------- DSP-ADPCM

def _clamp16(x):
    return -32768 if x < -32768 else 32767 if x > 32767 else x

def _lpc2(frames):
    """The best 2nd-order predictor (c1, c2) for a set of (history2, history1, samples) frames, by least squares."""
    r11 = r22 = r12 = b1 = b2 = 0.0
    for h2, h1, s in frames:
        p1, p2 = h1, h2
        for x in s:
            r11 += p1 * p1; r22 += p2 * p2; r12 += p1 * p2
            b1 += x * p1; b2 += x * p2
            p2, p1 = p1, x
    det = r11 * r22 - r12 * r12
    if abs(det) < 1e-6 * (r11 * r22 + 1):
        return (b1 / r11 if r11 else 0.0), 0.0
    return (b1 * r22 - b2 * r12) / det, (b2 * r11 - b1 * r12) / det

def dsp_coefs(samples):
    """Eight predictor pairs (as s16, 11 fractional bits) suited to these samples: each 14-sample frame's own best
    predictor, grouped into eight by k-means, weighted by the frame's loudness."""
    frames = []
    for i in range(0, len(samples), 14):
        s = samples[i:i + 14]
        h1 = samples[i - 1] if i >= 1 else 0
        h2 = samples[i - 2] if i >= 2 else 0
        e = sum(x * x for x in s)
        if e:
            frames.append((h2, h1, s, e))
    base = [(0.0, 0.0), (1.0, 0.0), (2.0, -1.0), (1.8, -0.82), (1.5, -0.6), (0.9, 0.0), (1.95, -0.96), (0.5, 0.25)]
    if not frames:
        cs = base
    else:
        step = max(1, len(frames) // 4000)  # long sounds: fit on a sample of the frames
        fs = frames[::step]
        preds = [(_lpc2([(h2, h1, s)]), (h2, h1, s), e) for h2, h1, s, e in fs]
        cs = list(base)
        for _ in range(6):
            groups = [[] for _ in cs]
            for (c1, c2), fr, e in preds:
                k = min(range(len(cs)), key=lambda j: (cs[j][0] - c1) ** 2 + (cs[j][1] - c2) ** 2)
                groups[k].append(fr)
            cs = [(_lpc2(g) if g else cs[j]) for j, g in enumerate(groups)]
    out = []
    for c1, c2 in cs:
        c1 = max(-3.99, min(3.99, c1)); c2 = max(-1.99, min(1.99, c2))
        out += [int(round(c1 * 2048)), int(round(c2 * 2048))]
    return out

def dsp_encode(samples, coefs):
    """Encode 16-bit samples as DSP-ADPCM (8-byte frames of 14 samples): for each frame, the predictor and scale
    that decode closest to the original. Returns (bytes, first frame's header byte)."""
    out = bytearray()
    h1 = h2 = 0
    first_ps = None
    pairs = [(coefs[2 * k], coefs[2 * k + 1]) for k in range(8)]
    for i in range(0, len(samples), 14):
        s = list(samples[i:i + 14])
        s += [0] * (14 - len(s))
        best = None
        for k, (c1, c2) in enumerate(pairs):
            # the scale the biggest prediction error needs, then that and the next one up
            a, b, peak = h1, h2, 0
            for x in s:
                e = x - ((c1 * a + c2 * b + 1024) >> 11)
                peak = max(peak, abs(e))
                b, a = a, x
            exp = 0
            while exp < 12 and (7 << exp) < peak:
                exp += 1
            for ex in (exp, min(12, exp + 1)) if exp < 12 else (12,):
                scale = 1 << ex
                a, b, err, nib = h1, h2, 0, []
                for x in s:
                    pred = c1 * a + c2 * b
                    n = int(round(((x << 11) - pred - 1024) / (scale << 11)))
                    n = -8 if n < -8 else 7 if n > 7 else n
                    y = _clamp16(((n * scale) << 11) + 1024 + pred >> 11)
                    err += (x - y) ** 2
                    if best is not None and err >= best[0]:
                        break
                    nib.append(n & 15)
                    b, a = a, y
                else:
                    best = (err, (k << 4) | ex, nib, a, b)
        _, ps, nib, h1, h2 = best
        if first_ps is None:
            first_ps = ps
        out.append(ps)
        out += bytes((nib[j] << 4) | nib[j + 1] for j in range(0, 14, 2))
    return bytes(out), first_ps or 0

def read_wav(data):
    """(sample rate, [channel sample lists]) from a WAV file: 8, 16, 24 or 32-bit PCM."""
    with wave.open(io.BytesIO(data)) as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        vals = struct.unpack('<%dh' % (len(raw) // 2), raw)
    elif width == 1:
        vals = [(b - 128) << 8 for b in raw]
    elif width == 3:
        vals = [int.from_bytes(raw[i:i + 3], 'little', signed=True) >> 8 for i in range(0, len(raw), 3)]
    elif width == 4:
        vals = [v >> 16 for v in struct.unpack('<%di' % (len(raw) // 4), raw)]
    else:
        raise ValueError('unsupported WAV sample size: %d bytes' % width)
    return rate, [list(vals[c::ch]) for c in range(ch)]

def make_wwav(orig, wav, progress=None):
    """A WWAV from a WAV file, as DSP-ADPCM. A sound that looped from start to end in the original still does."""
    rate, chans = read_wav(wav)
    if len(chans) > 2:
        raise ValueError('the game\'s sounds are mono or stereo; this WAV has %d channels' % len(chans))
    n = len(chans[0])
    ols, ole = struct.unpack('>II', orig[0x14:0x1C])
    on, = struct.unpack('>I', orig[0x10:0x14])
    ls, le = (0, n) if (ols, ole) == (0, on) else (min(ols, n), min(ole, n))
    ch = len(chans)
    info = 0x2C
    doff = align(info + 46 * ch) + 32
    nbytes = (n + 13) // 14 * 8
    stride = align(nbytes) + 64
    out = bytearray(orig[:0x0C])
    out += b'\0' * (doff + ch * stride - 32 - len(out))
    struct.pack_into('>fIIIHHIII', out, 0x0C, float(rate), n, ls, le, ch, 0, doff, stride, info)
    for c, samples in enumerate(chans):
        coefs = dsp_coefs(samples)
        data, ps = dsp_encode(samples, coefs)
        loop_ps = data[(ls // 14) * 8] if data else 0
        struct.pack_into('>16hHHhhHhh', out, info + c * 46, *coefs, 0, ps, 0, 0, loop_ps, 0, 0)
        out[doff + c * stride:doff + c * stride + len(data)] = data
        if progress:
            progress((c + 1) / ch)
    struct.pack_into('>I', out, 0x08, len(out))
    return bytes(out)

# ---------------------------------------------------------------- one file

KINDS = {b'WIMG': 'image', b'WTEX': 'texture', b'WANM': 'animation', b'TXTS': 'text', b'FONT': 'font',
         b'WWAV': 'sound', b'\x00\x20\xaf\x30': 'TPL texture', b'U\xaa8-': 'U8 archive'}

def kind(name, data):
    k = KINDS.get(data[:4])
    if k:
        return k
    ext = os.path.splitext(name)[1].lower()
    if ext in ('.cfg', '.txt', '.csv', '') and data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return 'UTF-16 text'
    return 'other'

def import_file(name, orig, path):
    """The game's version of an edited file (a PNG, CSV, WAV or text file at path) for archive entry `name`,
    whose current data is `orig`. Any other file type is used as it is."""
    ext = os.path.splitext(path)[1].lower()
    k = kind(name, orig)
    with open(path, 'rb') as f:
        data = f.read()
    if k == 'image' and ext != '.image':
        return make_wimg(orig, Image.open(path))
    if k == 'texture' and ext != '.texture':
        return make_wtex(orig, Image.open(path))
    if k == 'text' and ext == '.csv':
        return make_txts(orig, txts_changes(orig, read_csv(data)))
    if k == 'sound' and ext == '.wav':
        return make_wwav(orig, data)
    if k == 'UTF-16 text' and data[:2] not in (b'\xff\xfe', b'\xfe\xff'):
        return make_text16(orig, data)
    return data

# ---------------------------------------------------------------- packing a VolExtractor folder

def same_pixels(a, b):
    a, b = a.convert('RGBA'), b.convert('RGBA')
    if a.size != b.size:
        return False
    if a.tobytes() == b.tobytes():
        return True
    # where both are fully see-through, the colour doesn't matter
    pa, pb = a.load(), b.load()
    for y in range(a.height):
        for x in range(a.width):
            p, q = pa[x, y], pb[x, y]
            if p != q and not (p[3] == 0 and q[3] == 0):
                return False
    return True

def pack_folder(folder, vol_path=None, out_path=None, check_all=False, log=print):
    """Pack the edits in a VolExtractor folder (<name>_extracted, with raw/ and converted/) into a new .vol."""
    folder = os.path.abspath(folder.rstrip('/\\'))
    stem = os.path.basename(folder)
    if stem.endswith(wiivol.OUT_SUFFIX):
        stem = stem[:-len(wiivol.OUT_SUFFIX)]
    if vol_path is None:
        status = wiivol.read_status_any(folder)
        candidates = [status.get('vol_path')] if status else []
        candidates.append(os.path.join(os.path.dirname(folder), stem + '.vol'))
        vol_path = next((p for p in candidates if p and os.path.isfile(p)), None)
        if vol_path is None:
            raise FileNotFoundError('could not find the original %s.vol; give it with --vol' % stem)
    if out_path is None:
        out_path = os.path.join(os.path.dirname(folder), stem + '_new.vol')
    if os.path.abspath(out_path) == os.path.abspath(vol_path):
        log('Note: the original %s will be replaced.' % os.path.basename(vol_path))
    vol = Vol.load(vol_path)
    raw, conv = os.path.join(folder, 'raw'), os.path.join(folder, 'converted')
    status_file = os.path.join(folder, wiivol.STATUS_FILE)
    since = os.path.getmtime(status_file) if os.path.exists(status_file) and not check_all else None
    newer = lambda p: os.path.exists(p) and (since is None or os.path.getmtime(p) > since + 1)
    changed, skipped = [], []
    known = set()
    for name in vol.names():
        e = vol.find(name)
        orig = e['data']
        k = kind(name, orig)
        known.add(os.path.normcase(wiivol.safe_join(raw, name)))
        stem_c = os.path.splitext(wiivol.safe_join(conv, name))[0]
        new = None
        try:
            if k in ('image', 'texture') and newer(stem_c + '.png'):
                img = Image.open(stem_c + '.png')
                old = wiivol.wimg(orig) if k == 'image' else wiivol.wtex(orig)
                if not same_pixels(img, old):
                    new = (make_wimg if k == 'image' else make_wtex)(orig, img)
            elif k == 'animation' and os.path.isdir(stem_c):
                old, _, _ = wiivol.wanm(orig)
                frames, any_new = [], False
                for i, of in enumerate(old):
                    p = os.path.join(stem_c, 'frame_%02d.png' % i)
                    if newer(p) and of is not None and not same_pixels(Image.open(p), of):
                        frames.append(Image.open(p)); any_new = True
                    else:
                        frames.append(None)
                if any_new:
                    new = make_wanm(orig, frames)
            elif k == 'text' and newer(stem_c + '.csv'):
                with open(stem_c + '.csv', 'rb') as f:
                    ch = txts_changes(orig, read_csv(f.read()))
                if ch:
                    new = make_txts(orig, ch)
            elif k == 'sound' and newer(stem_c + '.wav'):
                with open(stem_c + '.wav', 'rb') as f:
                    data = f.read()
                if read_wav(data) != read_wav(wiivol.wwav(orig)):
                    log('  encoding %s...' % name)
                    new = make_wwav(orig, data)
            elif k == 'UTF-16 text':
                p = stem_c + os.path.splitext(name)[1]
                if newer(p):
                    with open(p, 'rb') as f:
                        new = make_text16(orig, f.read())
                    if new == orig or new.decode('utf-16') == orig.decode('utf-16'):
                        new = None
            elif k in ('font', 'TPL texture', 'U8 archive') and since is not None:
                p = stem_c + ('_glyphs.png' if k == 'font' else '.png' if k == 'TPL texture' else '')
                if p != stem_c and newer(p):
                    skipped.append('%s: a %s can\'t be packed from its converted file yet; replace it in raw/'
                                   % (name, k))
        except Exception as ex:
            raise RuntimeError('%s: %s' % (name, wiivol.describe(ex))) from ex
        rp = wiivol.safe_join(raw, name)
        if os.path.exists(rp) and (since is None or os.path.getmtime(rp) > since + 1):
            with open(rp, 'rb') as f:
                rd = f.read()
            if rd != orig:
                if new is not None:
                    skipped.append('%s: changed in both raw/ and converted/; the converted file was used' % name)
                else:
                    new = rd
        if new is not None and new != orig:
            vol.replace(name, new)
            changed.append(name)
    added = []
    if os.path.isdir(raw):
        for root, _, files in os.walk(raw):
            for f in sorted(files):
                p = os.path.join(root, f)
                if os.path.normcase(p) not in known:
                    name = os.path.relpath(p, raw).replace(os.sep, '/')
                    with open(p, 'rb') as fh:
                        vol.add(name, fh.read())
                    added.append(name)
    vol.save(out_path)
    log('%s: %d changed, %d added -> %s' % (os.path.basename(vol_path), len(changed), len(added), out_path))
    for n in changed:
        log('  changed  %s' % n)
    for n in added:
        log('  added    %s' % n)
    for s in skipped:
        log('  NOTE     %s' % s)
    return out_path, changed, added

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='Pack edited VolExtractor folders back into .vol archives.')
    ap.add_argument('folders', nargs='+', help='<name>_extracted folders made by VolExtractor')
    ap.add_argument('--vol', help='the original .vol (default: <name>.vol next to the folder)')
    ap.add_argument('-o', '--out', help='the new .vol (default: <name>_new.vol next to the folder)')
    ap.add_argument('--all', action='store_true',
                    help='compare every converted file with the original, not only those changed since extracting')
    args = ap.parse_args(argv[1:])
    if len(args.folders) > 1 and (args.vol or args.out):
        ap.error('--vol and -o can only be used with one folder')
    ok = True
    for folder in args.folders:
        try:
            pack_folder(folder, args.vol, args.out, args.all)
        except Exception as e:
            print('%s: FAILED (%s)' % (folder, e))
            ok = False
    return 0 if ok else 1

if __name__ == '__main__':
    sys.exit(main(sys.argv))
