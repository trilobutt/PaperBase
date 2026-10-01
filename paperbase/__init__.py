"""PaperBase.

Importing the package pins the process's C++ runtime before PyQt6 can. The PyQt6 wheel
bundles msvcp140.dll from MSVC 14.26 in PyQt6/Qt6/bin, and Windows binds every later load
of a DLL name to the module of that name already in the process, so once Qt is imported,
torch's c10.dll gets that 2020 runtime and fails its initialisation (WinError 1114). The
System32 copy is the VC++ 2015-2022 redistributable, which serves binaries from every
older toolset, so loading it first satisfies Qt and torch both. This must run before
anything imports PyQt6, which is why it sits here rather than in main: the tools and tests
import paperbase.* without going through main.
"""
import ctypes
import logging
import os
from pathlib import Path

_SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"

# The C++ runtime DLLs Qt6/bin carries that python.exe does not already load from its own
# directory. All four, so the process never mixes companion DLLs from two versions.
for _name in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "concrt140.dll"):
    try:
        ctypes.WinDLL(str(_SYSTEM32 / _name))
    except OSError as _e:
        # Without the redistributable torch cannot load either, and EmbeddingCategoriser
        # already degrades to keywords and taxa when the model fails to load.
        logging.getLogger(__name__).warning("C++ runtime %s not preloaded: %s", _name, _e)
