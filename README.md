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
4. You can optionally set an output folder where all animations will be saved.

   > If no output folder is set, FBX files will be downloaded to the folder where the program is running.

5. Press the `Start download` button and wait until it's done.
6. You can cancel the process at any time by pressing the `Stop` button.

> [!IMPORTANT]
> Downloading all animations can be quite slow. We're dealing with a total of 2346 animations, so don't expect it to be lighting fast.

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
