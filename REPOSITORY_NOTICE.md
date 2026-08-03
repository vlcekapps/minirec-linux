# Repository and licensing notice

MiniRec for Linux is a public, accessibility-first desktop port of the Android
application. Copyright (C) 2026 Pavel Vlček.

MiniRec for Linux is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. SPDX-License-Identifier: GPL-3.0-or-later.

MiniRec for Linux is distributed without any warranty; without even the implied
warranty of merchantability or fitness for a particular purpose. The complete,
unmodified GNU General Public License version 3 is in `LICENSE`. The AppStream
XML metadata is also available under the permissive FSFAP metadata license
declared in those files.

The project links only to system-provided GTK, libadwaita and GStreamer
components. It does not copy the Android application's bundled LAME source or
its binary signal assets. Recording signals are synthesized at run time, so no
third-party sound file is stored in this repository or in the RPM.
