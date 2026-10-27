# Action Snapshots

Blender add-on that automatically creates a `.blend` copy for every new Undo History entry, named `<session> + <action>.blend`.

## Install
Edit > Preferences > Add-ons > Install from Disk > `action_snapshots.py`, then enable it.

## Output
Snapshots go in an `action_snapshots/` folder next to your `.blend` (or in Blender's temp dir if unsaved). Duplicate names get `(2)`, `(3)`, etc.

## How it works
Blender's Python API can't read Undo History, so the add-on reads the undo stack from its own process's memory (read-only, fails safely) and polls it every 0.1s.

It also let you have INFINITE UNDO and a clear history of your work so you know where you fucked up.

## Notes
- Tested on Windows + Blender 5.2 only. Linux/macOS are implemented but untested.
- Experimental: relies on Blender internals and may break between versions.
- One full `.blend` per action, so disk usage grows fast. There is no pruning.

## Blender save system piss me off.
