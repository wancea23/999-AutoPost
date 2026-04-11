"""
Build script — creates a Desktop shortcut to launch 999 AutoPost.
Run once:  python build.py

Uses pythonw.exe (the no-console Python launcher bundled with every Python
install) so the app opens silently, just like a native .exe would.
No PyInstaller required — playwright runs from the system Python where
it is already installed and working.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGO_PNG = ROOT / "assets" / "logo.png"
ICON = ROOT / "assets" / "logo.ico"
APP = ROOT / "app.py"

# ── Convert logo.png → logo.ico ──────────────────────────────────────────────
if LOGO_PNG.exists():
    print("Converting logo.png → logo.ico …")
    try:
        from PIL import Image

        src = Image.open(LOGO_PNG).convert("RGBA")
        w, h = src.size
        sq = max(w, h)
        square = Image.new("RGBA", (sq, sq), (0, 0, 0, 0))
        square.paste(src, ((sq - w) // 2, (sq - h) // 2), src)
        square.save(
            str(ICON),
            format="ICO",
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
        )
        print(f"  Saved {ICON}")
    except Exception as e:
        print(f"  [WARN] Could not create .ico: {e}")
        ICON = None
else:
    print("[WARN] assets/logo.png not found — shortcut will have no icon")
    ICON = None

# ── Find pythonw.exe ──────────────────────────────────────────────────────────
# pythonw.exe ships alongside python.exe in every Python installation and
# starts the interpreter without opening a console window.
pythonw = Path(sys.executable).with_name("pythonw.exe")
if not pythonw.exists():
    pythonw = Path(sys.executable)
    print(f"[WARN] pythonw.exe not found; using {pythonw.name} (brief console flash on open)")
else:
    print(f"Using {pythonw}")

# ── Create Desktop shortcut ───────────────────────────────────────────────────
desktop = Path.home() / "Desktop"
shortcut = desktop / "999AutoPost.lnk"
icon_str = str(ICON) if ICON and ICON.exists() else ""

ps_script = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{shortcut}')
$Shortcut.TargetPath   = '{pythonw}'
$Shortcut.Arguments    = '"{APP}"'
$Shortcut.WorkingDirectory = '{ROOT}'
$Shortcut.IconLocation = '{icon_str}'
$Shortcut.Description  = '999 AutoPost'
$Shortcut.Save()
"""

print("Creating Desktop shortcut …")
try:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        check=True, capture_output=True,
    )
    print(f"\n✓ Done!")
    print(f"  Shortcut : {shortcut}")
    print(f"  Runs     : {pythonw.name} \"{APP}\"")
    print(f"  Work dir : {ROOT}")
    print("\nDouble-click '999AutoPost' on your Desktop to launch the app.")
except Exception as e:
    print(f"[ERROR] Could not create shortcut: {e}")
    sys.exit(1)
