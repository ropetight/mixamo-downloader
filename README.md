# Mixamo Downloader
GUI to bulk download animations from [Mixamo](https://www.mixamo.com/).

This repository contains both the Python source code (in the `/src` folder) and an `.exe` file (in the `/dist` folder) to make things easier to Windows users.

> [!NOTE]
> The `.exe` in `/dist` is the older PySide2 build. The Python sources have moved to PySide6 and gained resume, token refresh and a working Stop button; run from source to get those.

### For Python users

Make sure you have [Python 3.10+](https://www.python.org/) installed, then:

```bash
pip install -r requirements.txt   # PySide6 (with QtWebEngine) and requests
python src/main.pyw
```

Double-clicking `main.pyw` works too.

Your Mixamo session stays in the embedded browser: the access token is read from
it in memory and sent only to `mixamo.com`. Nothing is written outside the output
folder you choose.

### For non-technical users
If you don't have Python installed on your computer or you don't want to mess with all that coding stuff, download the `/dist` folder to your computer (~300MB) and run the `mixamo_downloader.exe`.

## How to use the Mixamo Downloader

1. Log into your Mixamo account in the embedded browser.
2. Select/upload the character you want to animate.
3. Choose between downloading `All animations`, `Animations containing the word` and the `T-Pose (with skin)`.
4. Check the `Download options` panel: format, skin, frame rate and keyframe reduction, the same fields Mixamo's own download dialog offers.
5. You can optionally set an output folder where all animations will be saved.

   > If no output folder is set, FBX files will be downloaded to the folder where the program is running.

6. Press the `Start download` button and wait until it's done.
7. You can cancel the process at any time by pressing the `Stop` button.

The controls sit in three columns under the browser -- what to download, how to export it, and where it goes with the buttons and progress -- and the log runs across the bottom. Everything is inside a splitter: drag the handle above the controls to give the Mixamo page more of the window, untick **Log** to fold the log away, and the position is remembered along with the window size.

> [!IMPORTANT]
> Downloading all animations can be quite slow. We're dealing with a total of 2346 animations, so don't expect it to be lighting fast.

## Export settings

Animations are requested from Mixamo with these preferences, matching the fields in Mixamo's own download dialog:

| Setting | Default | Choices |
|---|---|---|
| Format | `FBX Binary(.fbx)` | `FBX Binary(.fbx)`, `FBX ASCII(.fbx)`, `FBX for Unity(.fbx)`, `FBX 7.4(.fbx)`, `FBX 6.1(.fbx)`, `Collada(.dae)` |
| Skin | `Without Skin` | `Without Skin`, `With Skin` |
| Frames per Second | `30` | `24`, `30`, `60` |
| Keyframe Reduction | `none` | `none`, `uniform`, `non-uniform` |
| Pose | `T-pose` | `T-pose`, `Original Pose` — T-Pose download only |

The labels and the values sent to the API are taken verbatim from the table in Mixamo's own front-end bundle, so every option is one its own client sends. These are also Mixamo's own defaults, with one deliberate exception: it defaults to *With Skin*, this tool to *Without Skin*, since a bulk animation download rarely wants 2346 copies of the mesh.

The T-Pose is the one exception: it is exported *with* skin, since that is the only way to get the mesh, and it is the only download the `Pose` field applies to.

The file extension follows the format (`.dae` for Collada), and so does resume: it looks for the extension the selected format produces. Every download is checked against the signature its format should have — `Kaydara FBX Binary` for the binary FBX formats, `; FBX` for ASCII, `<?xml` for Collada — before the file is kept. An error page, a login redirect or an unexpected archive is reported instead of being saved under an export's name and then skipped by resume on the next run.

All of them are dropdowns in the **Download options** panel, populated from the values Mixamo is known to accept (`PREFERENCE_CHOICES` in `src/mixamo/client.py`) and preselected with the defaults above. There is no free text, so a run cannot be started with a value the API would reject, and anything unrecognised coming from a saved setting falls back to the default before it reaches Mixamo. **Reset** puts the panel back to the defaults.

The panel is locked while a download is running -- changing it mid-run would not affect the run -- and the settings actually used are written to the log when the run starts.

Only the defaults have been exercised against live Mixamo. The other values are Mixamo's own, but how a particular rig behaves in, say, FBX 6.1 has not been checked — worth one animation as a trial before a 2346-file batch.

> [!NOTE]
> Changing the frame rate does not re-download animations you already have — resume skips them by name. Delete the ones you want re-exported, or untick **Resume**.

## Resuming a download

Every finished animation is recorded in a `.mixamo_downloader.json` manifest inside the output folder, keyed by character. Leave **Resume** ticked and the next run skips whatever is already there — including FBX files downloaded before the manifest existed, and including a run that was stopped, crashed or lost its session half-way.

Animations that failed are recorded with their reason and are retried on the next run. Untick **Resume** to force a full re-download.

## What happens when things go wrong

| Problem | What the tool does |
|---|---|
| Access token expires mid-run | Detects it (proactively from the token's `exp`, or on a 401), asks the browser for a fresh one and replays the request |
| Not logged into Mixamo | Refuses to start and says so, instead of silently doing nothing |
| No primary character selected | Same: an explicit message naming the fix |
| Mixamo returns 429/5xx or the connection drops | Retries with exponential backoff, honouring `Retry-After` |
| An export job fails | Records that animation as failed and moves to the next one |
| An export job never finishes | Gives up after 5 minutes instead of polling forever |
| 10 animations fail in a row | Stops the run — something is wrong with the session, not the animation |
| Stop is pressed | Interrupts within a second, mid-download if needed; no truncated FBX is left behind |

Progress, warnings and errors are shown in the log panel at the bottom of the window and, where the desktop supports it, as a notification when the run ends.

Closing the window ends the process. A running download is stopped first (after asking), then the tray icon and the embedded browser are released explicitly -- either one will otherwise keep Qt's event loop alive with the window gone -- and the application object is dropped so Qt runs its own shutdown.

That is the graceful path, and it is the one that runs: the interpreter exits normally, `atexit` handlers run, and a frozen build cleans up its unpacked temporary folder. A watchdog on a daemon thread only steps in if shutdown has not finished after 10 seconds, which is the case where QtWebEngine has left a thread behind and the process would otherwise never return the shell prompt. It says so on stderr when it fires.

`Ctrl+C` in the terminal works too: the default SIGINT handler is restored, on Windows as well, so the key is not swallowed by the event loop.

## Platform notes

Nothing in the tool is tied to Linux:

- Downloaded file names avoid every character Windows rejects, the device names it reserves (`CON`, `NUL`, `COM1`...), and trailing dots and spaces it silently strips -- which would otherwise give the same animation two different names across platforms and confuse resume.
- Files are written through a temporary `.part` and an atomic replace, and the resume manifest the same way, both of which behave on NTFS as they do elsewhere.
- Shipped files (the animation list, the icon) are found through `sys._MEIPASS` and the executable's own folder before the source tree, so PyInstaller one-file and one-folder builds work without a code change.
- The output folder is opened through `QDesktopServices`, so it uses Explorer, Finder or the desktop's file manager as appropriate.

## Layout

```
src/
  main.pyw       entry point
  ui.py          main window: options, progress, log, notifications
  downloader.py  Qt worker bridging the UI and the core
  webpage.py     embedded browser page; reads the access token
  mixamo/        Qt-free core
    client.py    Mixamo API calls, retries, timeouts, streamed downloads
    tokens.py    access token storage, expiry and refresh
    state.py     resume manifest
    job.py       orchestration of one run
    errors.py    exception hierarchy
  tests/         pytest suite (no network, no display)
```

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite fakes the network and the filesystem, so it runs offline in about a second.
