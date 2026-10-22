"""VolKit: open a SpongeBob SquigglePants (Wii) .vol archive, look inside, change files and save it again.

Run:    python volkit.pyw [file.vol]      (or double-click VolKit.bat on Windows; volkit.sh on Mac or Linux)
Needs:  Python 3.8+ with Tkinter (part of Python on Windows and Mac) and Pillow; wiivol.py and volpack.py next to it

Pick a file on the left to see it on the right: pictures, animations (played with their own timing), text in every
language, fonts and sounds. Replace a file with a PNG, CSV, WAV or any file and it's converted on the spot; text
can be edited right in the window (double-click a line). Save writes the archive; nothing changes on disk before.
"""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volpack  # noqa: E402
import wiivol  # noqa: E402

TITLE = 'VolKit'
CHECKER = (64, 64, 72), (84, 84, 94)


def checker(size, cell=8):
    """A checkerboard to show see-through parts against."""
    im = Image.new('RGB', size, CHECKER[0])
    tile = Image.new('RGB', (cell * 2, cell * 2), CHECKER[0])
    tile.paste(CHECKER[1], (cell, 0, cell * 2, cell)); tile.paste(CHECKER[1], (0, cell, cell, cell * 2))
    for y in range(0, size[1], cell * 2):
        for x in range(0, size[0], cell * 2):
            im.paste(tile, (x, y))
    return im


def human(n):
    for unit in ('bytes', 'KB', 'MB'):
        if n < 1024 or unit == 'MB':
            return ('%d %s' % (n, unit)) if unit == 'bytes' else ('%.1f %s' % (n, unit))
        n /= 1024


class App:
    def __init__(self, root, path=None):
        self.root = root
        self.vol = None
        self.path = None
        self.dirty = False
        self.current = None
        self.photo = None
        self.anim_job = None
        self.jobs = queue.Queue()
        self.busy = False
        root.title(TITLE)
        root.geometry('1100x680')
        root.minsize(760, 460)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        self.build_ui()
        root.after(100, self.poll)
        if path:
            root.after(50, lambda: self.open(path))

    # ------------------------------------------------------------ layout

    def build_ui(self):
        style = ttk.Style()
        if 'clam' in style.theme_names() and sys.platform.startswith('linux'):
            style.theme_use('clam')
        bar = ttk.Frame(self.root, padding=(6, 6, 6, 0))
        bar.pack(fill='x')
        self.buttons = {}
        for key, text, cmd in (('open', 'Open .vol...', self.ask_open), ('save', 'Save', self.save),
                               ('saveas', 'Save As...', self.save_as), (None, None, None),
                               ('export', 'Export...', self.export), ('exportraw', 'Export Raw...', self.export_raw),
                               ('exportall', 'Export All...', self.export_all), (None, None, None),
                               ('replace', 'Replace...', self.replace), ('add', 'Add File...', self.add),
                               ('revert', 'Revert', self.revert)):
            if key is None:
                ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=6)
                continue
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side='left', padx=2)
            self.buttons[key] = b

        panes = ttk.PanedWindow(self.root, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=6, pady=6)

        left = ttk.Frame(panes)
        self.search = tk.StringVar()
        self.search.trace_add('write', lambda *_: self.fill_list())
        row = ttk.Frame(left)
        row.pack(fill='x', pady=(0, 4))
        ttk.Label(row, text='Find:').pack(side='left')
        ttk.Entry(row, textvariable=self.search).pack(side='left', fill='x', expand=True, padx=(4, 0))
        cols = ('type', 'size', 'state')
        self.tree = ttk.Treeview(left, columns=cols, selectmode='extended')
        self.tree.heading('#0', text='File')
        self.tree.heading('type', text='Type')
        self.tree.heading('size', text='Size')
        self.tree.heading('state', text='')
        self.tree.column('#0', width=200)
        self.tree.column('type', width=80, stretch=False)
        self.tree.column('size', width=70, stretch=False, anchor='e')
        self.tree.column('state', width=60, stretch=False)
        self.tree.tag_configure('changed', foreground='#c05800')
        self.tree.tag_configure('added', foreground='#1a7f37')
        sb = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.show())
        panes.add(left, weight=2)

        self.right = ttk.Frame(panes)
        self.info = ttk.Label(self.right, text='Open a .vol to begin.', font=('TkDefaultFont', 11, 'bold'))
        self.info.pack(fill='x')
        self.sub = ttk.Label(self.right, text='', foreground='#555')
        self.sub.pack(fill='x', pady=(0, 6))
        self.body = ttk.Frame(self.right)
        self.body.pack(fill='both', expand=True)
        panes.add(self.right, weight=3)
        self.root.after(150, lambda: panes.sashpos(0, 430))
        self.right.bind('<Configure>', lambda e: (self.info.configure(wraplength=max(200, e.width - 10)),
                                                  self.sub.configure(wraplength=max(200, e.width - 10))))

        bottom = ttk.Frame(self.root, padding=(6, 0, 6, 6))
        bottom.pack(fill='x')
        self.status = ttk.Label(bottom, text='')
        self.status.pack(side='left', fill='x', expand=True)
        self.progress = ttk.Progressbar(bottom, mode='indeterminate', length=160)
        self.update_buttons()

    def update_buttons(self):
        have = self.vol is not None
        sel = bool(self.tree.selection())
        for k, on in (('save', have and self.dirty), ('saveas', have), ('export', sel), ('exportraw', sel),
                      ('exportall', have), ('replace', sel and len(self.tree.selection()) == 1), ('add', have),
                      ('revert', sel)):
            self.buttons[k].state(['!disabled'] if on and not self.busy else ['disabled'])
        name = os.path.basename(self.path) if self.path else ''
        self.root.title('%s%s - %s' % ('*' if self.dirty else '', name, TITLE) if name else TITLE)

    def set_status(self, text):
        self.status.configure(text=text)

    # ------------------------------------------------------------ background work

    def run(self, label, work, done):
        """Run work() off the window's thread (long encodes), then done(result) back on it."""
        self.busy = True
        self.update_buttons()
        self.set_status(label)
        self.progress.pack(side='right')
        self.progress.start(12)

        def go():
            try:
                self.jobs.put((done, work(), None))
            except Exception as e:
                self.jobs.put((done, None, e))
        threading.Thread(target=go, daemon=True).start()

    def poll(self):
        try:
            while True:
                done, result, err = self.jobs.get_nowait()
                self.busy = False
                self.progress.stop()
                self.progress.pack_forget()
                if err is not None:
                    self.set_status('')
                    messagebox.showerror(TITLE, wiivol.describe(err))
                else:
                    done(result)
                self.update_buttons()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # ------------------------------------------------------------ files

    def confirm_discard(self):
        if not self.dirty:
            return True
        r = messagebox.askyesnocancel(TITLE, 'Save the changes to %s first?' % os.path.basename(self.path))
        if r is None:
            return False
        if r:
            return self.save()
        return True

    def ask_open(self):
        if not self.confirm_discard():
            return
        p = filedialog.askopenfilename(title='Open a .vol', filetypes=[('VOL archives', '*.vol'), ('All files', '*')])
        if p:
            self.open(p)

    def open(self, path):
        try:
            self.vol = volpack.Vol.load(path)
        except Exception as e:
            messagebox.showerror(TITLE, '%s isn\'t a .vol archive this can read.\n\n%s' % (path, e))
            return
        self.path = path
        self.dirty = False
        self.fill_list()
        self.clear_body()
        self.info.configure(text=os.path.basename(path))
        self.sub.configure(text='%d files. Pick one to see it.' % len(self.vol.names()))
        self.set_status('Opened %s' % path)
        self.update_buttons()

    def save(self):
        if self.vol is None:
            return False
        if os.path.exists(self.path) and not getattr(self, 'saved_once', False):
            if not messagebox.askokcancel(TITLE, 'Overwrite %s?\n\nKeep a copy of the original somewhere safe: the '
                                          'game needs its own files to run.' % os.path.basename(self.path)):
                return False
        return self.write(self.path)

    def save_as(self):
        if self.vol is None:
            return False
        base = os.path.splitext(os.path.basename(self.path))[0]
        p = filedialog.asksaveasfilename(title='Save the .vol as', defaultextension='.vol', initialfile=base + '_new.vol',
                                         filetypes=[('VOL archives', '*.vol')])
        if not p:
            return False
        ok = self.write(p)
        if ok:
            self.path = p
            self.update_buttons()
        return ok

    def write(self, path):
        try:
            self.vol.save(path)
        except Exception as e:
            messagebox.showerror(TITLE, 'Could not save %s:\n\n%s' % (path, e))
            return False
        self.saved_once = True
        self.dirty = False
        self.set_status('Saved %s' % path)
        self.update_buttons()
        return True

    def quit(self):
        if self.confirm_discard():
            self.root.destroy()

    # ------------------------------------------------------------ the list

    def fill_list(self):
        self.tree.delete(*self.tree.get_children())
        if self.vol is None:
            return
        q = self.search.get().lower()
        for name in sorted(self.vol.names(), key=str.lower):
            if q and q not in name.lower():
                continue
            self.insert_row(name)

    def insert_row(self, name):
        e = self.vol.find(name)
        added = e in self.vol.added
        state = 'added' if added else 'changed' if e['data'] != e['orig'] else ''
        self.tree.insert('', 'end', iid=name, text=name, tags=(state,) if state else (),
                         values=(volpack.kind(name, e['data']), human(len(e['data'])), state))

    def refresh_row(self, name):
        e = self.vol.find(name)
        added = e in self.vol.added
        state = 'added' if added else 'changed' if e['data'] != e['orig'] else ''
        if self.tree.exists(name):
            self.tree.item(name, tags=(state,) if state else (),
                           values=(volpack.kind(name, e['data']), human(len(e['data'])), state))

    def selected(self):
        return list(self.tree.selection())

    def changed(self, name, data):
        self.vol.replace(name, data)
        self.dirty = True
        self.refresh_row(name)
        self.update_buttons()
        if self.current == name:
            self.show(force=True)

    # ------------------------------------------------------------ preview

    def clear_body(self):
        if self.anim_job:
            self.root.after_cancel(self.anim_job)
            self.anim_job = None
        for w in self.body.winfo_children():
            w.destroy()
        self.photo = None

    def show(self, force=False):
        self.update_buttons()
        sel = self.selected()
        if len(sel) != 1:
            if len(sel) > 1:
                self.clear_body(); self.current = None
                self.info.configure(text='%d files picked' % len(sel))
                self.sub.configure(text='Export or Revert works on all of them.')
            return
        name = sel[0]
        if name == self.current and not force:
            return
        self.current = name
        self.clear_body()
        data = self.vol.get(name)
        k = volpack.kind(name, data)
        self.info.configure(text=name)
        self.sub.configure(text='%s, %s' % (k, human(len(data))))
        try:
            if k == 'image':
                img = wiivol.wimg(data)
                self.sub.configure(text='Image, %dx%d, GX format %d%s. Replace it with a PNG.'
                                   % (img.width, img.height, data[5], ' with alpha' if data[6] != 0xFF else ''))
                self.picture(img)
            elif k == 'texture':
                img = wiivol.wtex(data)
                self.sub.configure(text='Texture, %dx%d, %d mipmap level(s). Replace it with a PNG.'
                                   % (img.width, img.height, data[7]))
                self.picture(img)
            elif k == 'TPL texture':
                self.picture(wiivol.tpl(data)[0])
            elif k == 'animation':
                self.animation(name, data)
            elif k == 'text':
                self.text_table(name, data)
            elif k == 'font':
                sheet, rows = wiivol.font(data)
                self.sub.configure(text='Font, %d letters. (Fonts can only be replaced as raw files.)' % len(rows))
                self.picture(sheet)
            elif k == 'sound':
                self.sound(name, data)
            elif k == 'UTF-16 text':
                self.plain_text(name, data)
            else:
                self.hexdump(data)
        except Exception as e:
            ttk.Label(self.body, text='Could not show this file:\n%s' % wiivol.describe(e), foreground='#a00').pack()

    def picture(self, img, holder=None):
        holder = holder or self.body
        canvas = tk.Canvas(holder, highlightthickness=0, background='#2b2b33')
        canvas.pack(fill='both', expand=True)
        state = {'img': img.convert('RGBA')}

        def draw(_=None):
            w, h = max(canvas.winfo_width(), 50), max(canvas.winfo_height(), 50)
            im = state['img']
            k = min((w - 20) / im.width, (h - 20) / im.height, 4.0)
            k = max(k, 0.05)
            size = (max(1, int(im.width * k)), max(1, int(im.height * k)))
            shown = im.resize(size, Image.NEAREST if k >= 2 else Image.LANCZOS)
            bg = checker(size).convert('RGBA')
            bg.alpha_composite(shown)
            canvas.delete('all')
            state['photo'] = ImageTk.PhotoImage(bg)
            canvas.create_image(w // 2, h // 2, image=state['photo'])
        canvas.bind('<Configure>', draw)
        state['draw'] = draw
        return canvas, state

    def animation(self, name, data):
        frames, seq, errors = wiivol.wanm(data)
        seqs = wiivol.wanm_sequences(data)
        fmt = data[0x18]
        self.sub.configure(text='Animation, %d frames, %d step(s), GX format %d. Replace frames with PNGs.'
                           % (len(frames), len(seq), fmt))
        top = ttk.Frame(self.body)
        top.pack(fill='x')
        canvas, state = self.picture(Image.new('RGBA', (8, 8)))
        ctl = ttk.Frame(self.body)
        ctl.pack(fill='x', pady=4)
        playing = tk.BooleanVar(value=True)
        frame_no = tk.IntVar(value=0)
        if not seq:
            seq = [(i, 0, 0) for i, f in enumerate(frames) if f is not None]
        left = max(px for _, px, _ in seq); topy = max(py for _, _, py in seq)
        right = max(frames[f].width - px for f, px, _ in seq); bottom = max(frames[f].height - py for f, _, py in seq)

        def compose(step):
            f, px, py = seq[step % len(seq)]
            c = Image.new('RGBA', (max(1, left + right), max(1, topy + bottom)))
            c.alpha_composite(frames[f], (left - px, topy - py))
            return c, f

        pos = {'i': 0}

        def tick():
            img, f = compose(pos['i'])
            state['img'] = img
            state['draw']()
            label.configure(text='step %d of %d: frame %d' % (pos['i'] % len(seq) + 1, len(seq), f))
            frame_no.set(f)
            if playing.get():
                pos['i'] += 1
                self.anim_job = self.root.after(66, tick)
            else:
                self.anim_job = None

        def toggle():
            playing.set(not playing.get())
            play.configure(text='Pause' if playing.get() else 'Play')
            if playing.get() and self.anim_job is None:
                tick()

        def step(d):
            if playing.get():
                toggle()
            pos['i'] = (pos['i'] + d) % len(seq)
            tick()

        play = ttk.Button(ctl, text='Pause', command=toggle, width=7)
        play.pack(side='left')
        ttk.Button(ctl, text='<', width=3, command=lambda: step(-1)).pack(side='left', padx=2)
        ttk.Button(ctl, text='>', width=3, command=lambda: step(1)).pack(side='left')
        label = ttk.Label(ctl, text='')
        label.pack(side='left', padx=8)
        ttk.Button(ctl, text='Replace This Frame...', command=lambda: self.replace_frame(name, frame_no.get())) \
            .pack(side='right')
        ttk.Button(ctl, text='Export Frame...', command=lambda: self.save_image(
            frames[frame_no.get()], '%s_frame_%02d.png' % (os.path.splitext(name)[0].split('/')[-1], frame_no.get()))) \
            .pack(side='right', padx=4)
        if len(seqs) > 1:
            ttk.Label(ctl, text='(%d sequences; the first is shown)' % len(seqs)).pack(side='right', padx=6)
        self.root.after(30, tick)

    def text_table(self, name, data):
        head, rows = wiivol.txts(data)
        self.sub.configure(text='Text, %d lines in %d languages. Double-click a line to edit it, or replace the '
                           'whole file with a CSV.' % (len(rows), len(head) - 1))
        frame = ttk.Frame(self.body)
        frame.pack(fill='both', expand=True)
        cols = head[1:]
        tv = ttk.Treeview(frame, columns=cols, show='headings')
        for c in cols:
            tv.heading(c, text=c)
            tv.column(c, width=220)
        flat = lambda s: s.replace('\r\n', ' / ').replace('\r', ' / ').replace('\n', ' / ')
        for i, r in enumerate(rows):
            tv.insert('', 'end', iid=str(i), values=[flat(s) for s in r[1:]])
        ys = ttk.Scrollbar(frame, orient='vertical', command=tv.yview)
        xs = ttk.Scrollbar(frame, orient='horizontal', command=tv.xview)
        tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tv.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tv.bind('<Double-1>', lambda e: tv.focus() and self.edit_line(name, int(tv.focus()), head, rows))
        tv.bind('<Return>', lambda e: tv.focus() and self.edit_line(name, int(tv.focus()), head, rows))

    def edit_line(self, name, i, head, rows):
        win = tk.Toplevel(self.root)
        win.title('Edit line %s' % rows[i][0])
        win.transient(self.root)
        win.geometry('620x600')
        boxes = []
        outer = ttk.Frame(win, padding=10)
        outer.pack(fill='both', expand=True)
        btns = ttk.Frame(outer)
        btns.pack(side='bottom', fill='x', pady=(10, 0))
        ttk.Label(outer, text='ID %s. Line breaks are kept as the game writes them.' % rows[i][0]).pack(anchor='w')
        for lang, text in zip(head[1:], rows[i][1:]):
            ttk.Label(outer, text=lang, font=('TkDefaultFont', 9, 'bold')).pack(anchor='w', pady=(6, 0))
            t = tk.Text(outer, height=2, wrap='word', undo=True)
            t.insert('1.0', text.replace('\r\n', '\n').replace('\r', '\n'))
            t.pack(fill='both', expand=True)
            boxes.append(t)

        def apply():
            data = self.vol.get(name)
            table = [[rows[i][0]] + [b.get('1.0', 'end-1c') for b in boxes]]
            ch = volpack.txts_changes(data, table)
            win.destroy()
            if ch:
                self.changed(name, volpack.make_txts(data, ch))
                self.set_status('Changed %d string(s) in %s' % (len(ch), name))
        ttk.Button(btns, text='Apply', command=apply).pack(side='right')
        ttk.Button(btns, text='Cancel', command=win.destroy).pack(side='right', padx=6)
        boxes[0].focus_set()

    def plain_text(self, name, data):
        self.sub.configure(text='UTF-16 text. Edit it here and press Apply.')
        t = tk.Text(self.body, wrap='none', undo=True)
        t.insert('1.0', data.decode('utf-16'))
        t.pack(fill='both', expand=True)

        def apply():
            new = volpack.make_text16(data, t.get('1.0', 'end-1c').encode('utf-8'))
            if new.decode('utf-16') != data.decode('utf-16'):
                self.changed(name, new)
        ttk.Button(self.body, text='Apply', command=apply).pack(anchor='e', pady=4)

    def sound(self, name, data):
        import struct
        rate, = struct.unpack('>f', data[0x0C:0x10])
        n, ls, le = struct.unpack('>III', data[0x10:0x1C])
        ch, = struct.unpack('>H', data[0x1C:0x1E])
        self.sub.configure(text='Sound, %s, %d Hz, %.2f seconds. Replace it with a WAV.'
                           % ('stereo' if ch == 2 else 'mono', rate, n / rate))
        box = ttk.Frame(self.body, padding=20)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='Nintendo DSP-ADPCM, %d samples%s.' % (
            n, ', loops' if le else '')).pack(anchor='w')
        row = ttk.Frame(box)
        row.pack(anchor='w', pady=10)
        ttk.Button(row, text='Play', command=lambda: self.play(data)).pack(side='left')
        ttk.Button(row, text='Export WAV...', command=self.export).pack(side='left', padx=6)
        ttk.Button(row, text='Replace with WAV...', command=self.replace).pack(side='left')

    def play(self, data):
        wav = wiivol.wwav(data)
        path = os.path.join(tempfile.gettempdir(), 'volkit_preview.wav')
        with open(path, 'wb') as f:
            f.write(wav)
        try:
            if sys.platform == 'win32':
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            elif sys.platform == 'darwin':
                subprocess.Popen(['afplay', path])
            else:
                for player in (['paplay', path], ['aplay', '-q', path], ['ffplay', '-nodisp', '-autoexit', path]):
                    try:
                        subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
                    except FileNotFoundError:
                        continue
                else:
                    self.set_status('No sound player found (paplay, aplay or ffplay); export the WAV instead.')
        except Exception as e:
            self.set_status('Could not play it: %s' % e)

    def hexdump(self, data):
        t = tk.Text(self.body, wrap='none', font=('Courier', 10))
        for off in range(0, min(len(data), 4096), 16):
            chunk = data[off:off + 16]
            t.insert('end', '%06X  %-48s %s\n' % (off, ' '.join('%02X' % b for b in chunk),
                                                 ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)))
        if len(data) > 4096:
            t.insert('end', '... (%d more bytes)\n' % (len(data) - 4096))
        t.configure(state='disabled')
        t.pack(fill='both', expand=True)

    # ------------------------------------------------------------ exporting

    def converted(self, name, data):
        """(file extension, bytes or PIL image) of the easy-to-open version of a file, or None."""
        k = volpack.kind(name, data)
        if k == 'image':
            return '.png', wiivol.wimg(data)
        if k == 'texture':
            return '.png', wiivol.wtex(data)
        if k == 'TPL texture':
            return '.png', wiivol.tpl(data)[0]
        if k == 'sound':
            return '.wav', wiivol.wwav(data)
        if k == 'text':
            import csv
            import io
            head, rows = wiivol.txts(data)
            buf = io.StringIO()
            csv.writer(buf).writerows([head] + rows)
            return '.csv', buf.getvalue().encode('utf-8-sig')
        if k == 'font':
            return '_glyphs.png', wiivol.font(data)[0]
        if k == 'UTF-16 text':
            return os.path.splitext(name)[1] or '.txt', data.decode('utf-16').encode('utf-8-sig')
        return None

    def write_out(self, path, thing):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        if isinstance(thing, Image.Image):
            thing.save(path)
        else:
            with open(path, 'wb') as f:
                f.write(thing)

    def save_image(self, img, default):
        p = filedialog.asksaveasfilename(title='Export', initialfile=default, defaultextension='.png',
                                         filetypes=[('PNG', '*.png')])
        if p:
            img.save(p)
            self.set_status('Exported %s' % p)

    def export(self):
        names = self.selected()
        if not names:
            return
        if len(names) == 1:
            name = names[0]
            data = self.vol.get(name)
            if volpack.kind(name, data) == 'animation':
                return self.export_anim(name, data)
            c = self.converted(name, data)
            base = os.path.splitext(os.path.basename(name))[0]
            ext, thing = c if c else (os.path.splitext(name)[1], data)
            p = filedialog.asksaveasfilename(title='Export %s' % name, initialfile=base + ext)
            if p:
                self.write_out(p, thing)
                self.set_status('Exported %s' % p)
            return
        d = filedialog.askdirectory(title='Export %d files to' % len(names))
        if d:
            self.run('Exporting...', lambda: self.export_many(names, d, True),
                     lambda n: self.set_status('Exported %d files to %s' % (n, d)))

    def export_anim(self, name, data):
        d = filedialog.askdirectory(title='Export the frames of %s to' % name)
        if not d:
            return
        frames, seq, _ = wiivol.wanm(data)
        base = os.path.splitext(os.path.basename(name))[0]
        for i, f in enumerate(frames):
            if f is not None:
                f.save(os.path.join(d, '%s_frame_%02d.png' % (base, i)))
        if seq:
            wiivol.anim_gif(frames, seq, os.path.join(d, base + '.gif'))
        self.set_status('Exported %d frames of %s to %s' % (len(frames), name, d))

    def export_raw(self):
        names = self.selected()
        if len(names) == 1:
            p = filedialog.asksaveasfilename(title='Export %s as it is stored' % names[0],
                                             initialfile=os.path.basename(names[0]))
            if p:
                self.write_out(p, self.vol.get(names[0]))
                self.set_status('Exported %s' % p)
        elif names:
            d = filedialog.askdirectory(title='Export %d files to' % len(names))
            if d:
                self.run('Exporting...', lambda: self.export_many(names, d, False),
                         lambda n: self.set_status('Exported %d files to %s' % (n, d)))

    def export_all(self):
        if self.vol is None:
            return
        d = filedialog.askdirectory(title='Export everything (raw and converted) to')
        if not d:
            return
        base = os.path.join(d, os.path.splitext(os.path.basename(self.path))[0] + wiivol.OUT_SUFFIX)

        def work():
            log = []
            for name in self.vol.names():
                data = self.vol.get(name)
                wiivol.write(wiivol.safe_join(os.path.join(base, 'raw'), name), data)
                wiivol.convert(name, data, os.path.join(base, 'converted'), log)
            if not self.dirty and self.path:
                wiivol.write_status(base, self.path, set())
            return len(self.vol.names()), log
        self.run('Exporting everything...', work,
                 lambda r: self.set_status('Exported %d files to %s%s' % (r[0], base, ' (%d problems)' % len(r[1])
                                                                                if r[1] else '')))

    def export_many(self, names, d, convert):
        for name in names:
            data = self.vol.get(name)
            c = self.converted(name, data) if convert else None
            if convert and volpack.kind(name, data) == 'animation':
                frames, seq, _ = wiivol.wanm(data)
                stem = wiivol.safe_join(d, os.path.splitext(name)[0])
                for i, f in enumerate(frames):
                    if f is not None:
                        self.write_out(os.path.join(stem, 'frame_%02d.png' % i), f)
                continue
            if c:
                self.write_out(os.path.splitext(wiivol.safe_join(d, name))[0] + c[0], c[1])
            else:
                self.write_out(wiivol.safe_join(d, name), data)
        return len(names)

    # ------------------------------------------------------------ changing

    def replace(self):
        names = self.selected()
        if len(names) != 1:
            return
        name = names[0]
        data = self.vol.get(name)
        k = volpack.kind(name, data)
        types = {'image': [('PNG pictures', '*.png'), ('Other pictures', '*.jpg *.jpeg *.bmp *.gif *.tga *.webp')],
                 'texture': [('PNG pictures', '*.png'), ('Other pictures', '*.jpg *.jpeg *.bmp *.gif *.tga *.webp')],
                 'text': [('CSV spreadsheets', '*.csv')], 'sound': [('WAV sounds', '*.wav')],
                 'UTF-16 text': [('Text files', '*.txt *.cfg')]}.get(k, [])
        p = filedialog.askopenfilename(title='Replace %s with' % name, filetypes=types + [('All files', '*')])
        if not p:
            return
        if k == 'animation':
            if not messagebox.askokcancel(TITLE, 'Use %s as the whole animation file?\n\nTo change one picture, use '
                                          '"Replace This Frame" under the preview.' % os.path.basename(p)):
                return
        label = 'Encoding the sound (long sounds take a while)...' if k == 'sound' else 'Converting...'
        self.run(label, lambda: volpack.import_file(name, data, p),
                 lambda new: (self.changed(name, new), self.set_status('Replaced %s with %s' % (name, os.path.basename(p)))))

    def replace_frame(self, name, i):
        p = filedialog.askopenfilename(title='Replace frame %d with' % i,
                                       filetypes=[('PNG pictures', '*.png'), ('All files', '*')])
        if not p:
            return
        data = self.vol.get(name)
        nframes = len(wiivol.wanm(data)[0])

        def work():
            frames = [None] * nframes
            frames[i] = Image.open(p)
            return volpack.make_wanm(data, frames)
        self.run('Converting...', work, lambda new: (self.changed(name, new),
                                                     self.set_status('Replaced frame %d of %s' % (i, name))))

    def add(self):
        if self.vol is None:
            return
        p = filedialog.askopenfilename(title='Add a file to the archive')
        if not p:
            return
        win = tk.Toplevel(self.root)
        win.title('Add a file')
        win.transient(self.root)
        f = ttk.Frame(win, padding=12)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='Name in the archive (the game finds files by this name):').pack(anchor='w')
        var = tk.StringVar(value=os.path.basename(p))
        e = ttk.Entry(f, textvariable=var, width=50)
        e.pack(fill='x', pady=6)

        def ok():
            name = var.get().strip().replace('\\', '/')
            if not name:
                return
            if self.vol.find(name) is not None:
                messagebox.showerror(TITLE, '%s is already in the archive. Use Replace to change it.' % name)
                return
            with open(p, 'rb') as fh:
                self.vol.add(name, fh.read())
            win.destroy()
            self.dirty = True
            self.fill_list()
            self.tree.selection_set(name)
            self.tree.see(name)
            self.set_status('Added %s' % name)
            self.update_buttons()
        row = ttk.Frame(f)
        row.pack(fill='x')
        ttk.Button(row, text='Add', command=ok).pack(side='right')
        ttk.Button(row, text='Cancel', command=win.destroy).pack(side='right', padx=6)
        e.focus_set()

    def revert(self):
        names = self.selected()
        for name in names:
            self.vol.revert(name)
        self.dirty = any(self.vol.changed(n) for n in self.vol.names())
        self.fill_list()
        for n in names:
            if self.tree.exists(n):
                self.tree.selection_add(n)
        self.show(force=True)
        self.set_status('Reverted %d file(s)' % len(names))


def main():
    root = tk.Tk()
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == '__main__':
    main()
