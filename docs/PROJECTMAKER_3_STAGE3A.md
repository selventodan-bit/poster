# Poster 3.0 — Stage 3A browser environment

Base: `f5c1d70c865081d2fc4c1dc923863bc77c85ec19`. Branch:
`codex/poster-3-browser-stage3a`. Previous release branches, PR #2 and main are unchanged.

## Architecture and functional reference

The existing Tk graph editor, GraphRunner, registry, debugger and HTTP architecture
are retained. `BrowserSession` delegates to GUI-independent Chromium/Qt providers.
The ordinary Linux CLI imports neither Tk nor PySide. Existing graphs still use
external Chromium by default; a GUI preference does not silently change CLI behavior.

Qt runs in an owned subprocess with a real QWebEngineView/QWebEngineProfile.
The editor and browser are **two coordinated windows**, not a docked single-window
ProjectMaker. Tk/Qt event loops are never mixed; SetParent is never used. A whole
editor migration would risk the working canvas/debugger and is deferred until
an equivalent native GUI scenario demonstrates parity. The Qt window has an address
bar, history, reload, tabs, popups and zoom. It is not an external Chrome window
relabeled as embedded Chromium.

The provider owns the host, communicates over inherited JSON-lines pipes with a
fixed command vocabulary, connects CDP only on loopback, and removes only its own
temporary directory on shutdown. The desktop encryption key is not passed to the host.
The desktop browser sandbox stays enabled. Root/Xvfb development tests explicitly
opt out with `POSTER_QT_TEST_NO_SANDBOX=1`; desktop code never sets this automatically.

Official ZennoLab documentation was reread as functional reference via its public
[documentation source](https://github.com/ZennoLab/docs/tree/main/docs/ZennoPoster/ProjectMaker):
Browser Window, Action Constructor and XPath Search, Browser tab management,
Profile Window, Using a Profile Folder, Recording settings, Creating a Project,
Starting Debugging, Re-recording Problematic Sections and Project Editor.
The relevant behaviors are recording into editable actions, targeting tab/frame,
checking element matches, resuming recording, and retaining state while debugging.
No vendor UI, assets or private code are copied.

## Graph and recording

Schema remains v2, with the existing read-only v1 migration. Existing Workflow v1
and READY projects are untouched. New recordings annotate browser nodes with
`extensions.browser_tab` (`main`, `tab_1`, …). IDs are logical: no machine/CDP IDs
are written to projects. A missing recorded tab fails explicitly instead of using
another page. Old index-based SwitchTab actions keep their meaning.

NewTab/SwitchTab accept optional logical IDs. WaitPopup waits for a new page from
the causal opener/Click checkpoint; it does not create another window or retry a
form. Recorder uses the existing DOM capture binding, records frames, popup actions
and subsequent tab switches. It does not use RecognitionSession's POST gate.
Passwords remain encrypted references before graph callbacks or saves.

Qt/CDP does not implement Target.createTarget, browser contexts, or Storage cookies
commands. Native Qt controls create tabs; the provider uses the supported Network
cookie commands for snapshots. A clipped iframe can acknowledge scroll before Qt
paints it; Qt pointer preparation waits animation frames and a compositor read
barrier before the single click. The barrier discards screenshot bytes in memory;
it never writes page contents or credentials to disk.

The element constructor reports CSS/XPath, attributes, match count, frame chain
and logical tab, applies a selector to a paused node, creates Click/Input/Wait or
ReadText/ReadAttribute nodes and uses the existing selected-action debugger.

## Profiles

Create/select a local profile before opening the browser. **Save session** explicitly
saves encrypted cookies and localStorage under `.poster/browser-profiles`, outside
project/Drive directories. Each run gets a fresh browser context/process restored
from an immutable snapshot, not a shared writable Chrome directory. Revision checking
rejects conflicting saves. Clear affects only the selected closed profile.

localStorage is seeded once using an intercepted synthetic document at each saved
origin; no bootstrap request reaches the site. It is not overwritten on later loads,
including after `localStorage.clear()`. Session cookies retain their session scope.
No automatic save on close occurs. IndexedDB, service worker state, sessionStorage,
client certificates, downloads and extension profiles are not portable snapshots.
Only profile references/settings belong in graph metadata; encryption keys never do.
On another computer select a new local profile and authorize normally.

## Windows distributions and checking

* `poster-windows`: Lite, installed Chrome/Chromium, no browser download.
* `poster-maker-windows`: explicit larger QtWebEngine distribution; defaults to Qt.

Both are portable windowed applications. Extract the **whole** ZIP to a writable
folder and double-click `Poster.exe`. No Python, Git, Docker or terminal is needed.
Choose **Demo site → Start recording**, use the browser, **Stop recording**, edit a
node, run and save. Full Maker does not need installed Chrome for Qt sessions;
the external provider still uses an installed Chrome/Chromium.

Each build writes BUILD_INFO, inner ZIP SHA256 and `build-report.json` with exact
ZIP/unpacked byte counts and source commit. Artifacts become release candidates
only after their native GUI checks succeed; an uploaded artifact alone is not proof.
The prior Lite ZIP was approximately 63.5 MB; compare actual build reports, not wheel sizes.

Developer checks: `pytest -q`, `ruff check .`, `ruff format --check .`.
The explicit native provider suite is
`POSTER_TEST_QT=1 pytest -q tests/test_maker_stage3a.py` on Windows or Xvfb Linux.
Windows packaging runs `scripts/build_windows_app.py --embedded` followed by the
same real OS-input graph/debugger scenario used for Lite, adapted to native Qt view
geometry. CI also retains all original CMS, editor, HTTP, database and Drive checks.

## Limits / next stage

This is a targeted browser integration, not complete ZennoLab parity. Two windows
remain. Browser-specific dialogs, file upload/download, browser permissions,
CAPTCHA/access controls, extensions, closed shadow roots and native browser UI
are not recorded as universal actions. No anti-bot bypass is implemented. Snapshots
have the storage limits above. Existing Qt 6.8.3 is pinned for reproducibility;
changing Qt/Chromium requires rerunning the compatibility and packaging suites.
Stage 3B should first consider a supported current Qt browser version and true
single-window docking/editor migration with OS-input parity, then richer profile
storage and explicit browser dialogs. Molab, scheduling and Engine redesign are out
of scope. See CI and the final delivery report for **executed** results and blockers.
