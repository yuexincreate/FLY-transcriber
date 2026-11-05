"""A ``FLY.app`` in the Applications folder, so the app can be opened like any other.

The app is otherwise only a command-line tool and a login item: once quit, the
menubar icon is gone with nothing in Spotlight or Launchpad to bring it back.
The bundle here is a stub that starts that tool with ``--show``. It has no Dock
icon (``LSUIElement``) and needs no signing beyond an ad-hoc one.

Installed in ``/Applications`` when the user may write there (admin users can,
no ``sudo`` involved) and in ``~/Applications`` otherwise.
"""

from __future__ import annotations

import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

APP_NAME = "FLY.app"
BUNDLE_ID = "io.github.fly-transcriber.app"
ICON = Path(__file__).parent / "assets" / "FLY.icns"
#: Tried in order; the first writable one wins.
APP_DIRS = (Path("/Applications"), Path("~/Applications").expanduser())
TOOL = Path("~/.local/bin/fly-transcriber").expanduser()


def install_launcher(dirs: tuple[Path, ...] | None = None, tool: Path | None = None) -> Path:
    """Create ``FLY.app`` in the first writable directory and return its path.

    Raises ``OSError`` when none is writable. Any earlier copy of ours is removed
    first, so moving between the two locations never leaves two FLYs behind.
    """
    dirs = APP_DIRS if dirs is None else dirs
    remove_launcher(dirs)
    last: OSError | None = None
    for directory in dirs:
        try:
            return _build(directory, tool or TOOL)
        except OSError as exc:  # typically PermissionError for /Applications
            last = exc
    raise last or OSError("no Applications folder to install into")


def remove_launcher(dirs: tuple[Path, ...] | None = None) -> list[Path]:
    """Delete our ``FLY.app`` from each directory; never touches anyone else's."""
    removed = []
    for directory in APP_DIRS if dirs is None else dirs:
        bundle = directory / APP_NAME
        if _is_ours(bundle):
            try:
                shutil.rmtree(bundle)
                removed.append(bundle)
            except OSError:
                pass  # e.g. root-owned; the caller can tell the user
    return removed


def _is_ours(bundle: Path) -> bool:
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier") == BUNDLE_ID
    except (OSError, plistlib.InvalidFileException):
        return False


def _tool_command(tool: Path) -> str:
    """The tool as the stub script names it, relative to ``$HOME`` when it can be.

    ``/Applications`` is shared by every account on the Mac. With the installing
    user's home spelled out, anyone else opening FLY there would run that user's
    copy, which that user could change; ``$HOME`` makes each person run their own.
    """
    try:
        relative = tool.relative_to(Path.home())
    except ValueError:
        return shlex.quote(str(tool))
    return f'"$HOME"/{shlex.quote(str(relative))}'


def _build(directory: Path, tool: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / APP_NAME
    # Built beside its destination, then renamed in, so a failure half way
    # never leaves a broken app where a working one was.
    staging = directory / f".{APP_NAME}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        macos = staging / "Contents" / "MacOS"
        resources = staging / "Contents" / "Resources"
        macos.mkdir(parents=True)
        resources.mkdir()

        script = macos / "FLY"
        # Started detached, not exec'd: when the app process *is* the one
        # LaunchServices launched for this bundle, macOS never shows its menubar
        # icon (seen on macOS 26; the cause is unknown). The script exits at
        # once, which is fine -- a second launch finds the running app itself.
        # `arch -arm64`: a bundle whose executable is a script has no
        # architecture of its own, so LaunchServices may start it under Rosetta,
        # and the Intel preference is inherited by everything FLY starts -- the
        # in-app update's install.sh then refused to run ("Intel mode").
        script.write_text(
            "#!/bin/sh\n"
            "# Starts FLY, or brings up the running one.\n"
            f"nohup /usr/bin/arch -arm64 {_tool_command(tool)} --show >/dev/null 2>&1 &\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        if ICON.exists():
            shutil.copy2(ICON, resources / "FLY.icns")

        with open(staging / "Contents" / "Info.plist", "wb") as handle:
            plistlib.dump(
                {
                    "CFBundleName": "FLY",
                    "CFBundleDisplayName": "FLY",
                    "CFBundleIdentifier": BUNDLE_ID,
                    "CFBundleExecutable": "FLY",
                    "CFBundleIconFile": "FLY",
                    "CFBundlePackageType": "APPL",
                    "CFBundleVersion": "1",
                    "CFBundleShortVersionString": "1",
                    "LSMinimumSystemVersion": "14.2",
                    "LSUIElement": True,  # the app draws its own menubar icon
                    "LSArchitecturePriority": ["arm64"],  # never under Rosetta
                },
                handle,
            )

        # Best effort: a signature is not required for a locally made script.
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(staging)],
            check=False, capture_output=True,
        )
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final
