# Mini Clipboard Manager (Windows)

Simple clipboard manager that keeps the **last 10 copied text items** and lets you select one to **paste** via a tiny GUI.

## Features

- Stores last 10 clipboard text entries (you can change the amount in the python file)
- GUI list with filter box
- Double-click / Enter to paste selected item
- **Global hotkey**: `Ctrl+Shift+V` toggles show/hide (currently broken)

## Install

From PowerShell in this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python clipboard_manager.py
```

## Notes / limitations

- Designed for **text** clipboard content.
- “Paste” is implemented by copying the selected item to the clipboard and sending `Ctrl+V`.
- Some apps may block simulated keypresses (try running the app as admin if needed).
- If you see an error about `tkinter`, this project uses **PySide6 (Qt)** instead (no Tkinter needed).

i will add an .exe for easy access in the **realeases** section
i am currently a student so i will not be able to work on this very much

