# VolExtractor

Unpacks the `.vol` archives of *SpongeBob SquigglePants* (Wii) and converts what's inside into files you can open: PNG images, animated GIFs, WAV sounds and CSV spreadsheets of the game's text.

Part of the [*SpongeBob SquigglePants+*](https://sbsp-plus.wubbzp.com/) project. It's the same code the patch's own tools use to read the game's files.

**No game files are included.** You need your own copy of the game.

## Which Games It Works With

It's made for, and tested with, *SpongeBob SquigglePants* on the Wii (US, `SS8E78`). The game was built with EngineBlack, the engine behind many of WayForward's games, and other games built with it may use some of the same formats, but none have been tested. Not all of them use `.vol` archives: *Batman: The Brave and the Bold - The Videogame* on the Wii, for one, doesn't. The 3DS and DS versions of EngineBlack's games would also store their numbers in the opposite byte order and use different image and sound formats, so they'd need changes to the code.

## What You Get

Each archive is unpacked into a `<name>_extracted` folder next to it:

```
Boot_extracted/
  raw/         every file exactly as stored in the archive
  converted/   the same files, converted where the format is known
```

| In the game | Converted to |
| --- | --- |
| `.image` (WIMG), `.texture` (WTEX), `.tpl` | PNG, with transparency |
| `.anim` (WANM) | One PNG per frame, plus an animated GIF |
| `.text` (TXTS) | CSV, one column per language (English, French, German, Italian, Dutch, Spanish) |
| `.font` (FONT) | A PNG sheet of every letter, plus a CSV of their sizes |
| `.wave` (WWAV, DSP-ADPCM) | WAV |
| `.arc` (U8 archives) | Unpacked, and their contents converted too |
| UTF-16 `.cfg` and `.txt` | UTF-8 text |

Files in other formats are still in `raw/`.

## Getting the Game's Files

The archives are on the game disc. In [Dolphin](https://dolphin-emu.org/): right-click the game, choose **Properties**, then **Filesystem**, then right-click the files or folders you want and extract them. On a Wii, back up your disc with CleanRip first and open the backup in Dolphin.

## Using It

You need [Python 3.8 or newer](https://www.python.org/downloads/). On Windows, tick "Add python.exe to PATH" when installing. The launchers install the one package it needs, [Pillow](https://pypi.org/project/pillow/), the first time.

**Windows:** put the `.vol` files (or folders of them) in this folder and double-click `extract_all.bat`, or drag files and folders onto it.

**macOS and Linux:**

```sh
./extract_all.sh                     # every .vol in this folder and its subfolders
./extract_all.sh ~/SquigglePants     # or the files and folders you name
```

**From the command line**, anywhere:

```sh
python3 -m pip install pillow
python3 wiivol.py [file.vol | folder ...] [-o output_folder] [-f]
```

| Option | What it does |
| --- | --- |
| *(no paths)* | Searches the current folder and its subfolders. |
| `-o folder` | Puts every archive's output in `folder/<name>/` instead of next to each `.vol`. |
| `-f` | Extracts everything again. Normally, archives a previous run finished are skipped, and only files that failed last time are retried (the progress is kept in `_wiivol_status.json` in each output folder). |

A whole disc's worth of archives takes a while and a lot of disc space: `Boot.vol` alone becomes about 17 MB.

## The File Formats

`wiivol.py` starts with notes on every format it reads (all big-endian): the DVOL archive and its hash table, WIMG and WTEX images with their run-length coded GX tiles, WANM animations and their sequences, TXTS text, FONT glyph tables and WWAV sounds. They're there for anyone who wants to write their own tools.

The game's own `.brlyt`, `.brlan`, `.brfnt`, `.bwav` and `.mid` files (inside the U8 archives) are standard Nintendo or MIDI formats, so they're copied out as they are.

## Reporting a Problem

If a file fails, the output names it, with the error and the line of `wiivol.py` it came from. Please include those lines when you [open an issue](https://github.com/spongebobsquigglepants/volextractor/issues).

## Credits

By Mitch "WubbzP" Bentz, for [*SpongeBob SquigglePants+*](https://sbsp-plus.wubbzp.com/).

<sub>A fan-made tool, not affiliated with or endorsed by Nickelodeon, THQ, WayForward or uDraw. *SpongeBob SquarePants*, *SpongeBob SquigglePants* and all related titles, logos and characters are trademarks of Viacom International Inc.</sub>
