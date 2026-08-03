# Repository and licensing notice

MiniRec for Linux is a public, accessibility-first desktop port of the Android
application. Copyright (C) 2026 Pavel Vlček. The source code, original artwork
and documentation are free software licensed under GPL-3.0-or-later; the full
terms are in `LICENSE`. The AppStream XML metadata is also available under the
permissive FSFAP metadata license declared in those files.

The project links only to system-provided GTK, libadwaita and GStreamer
components. It does not copy the Android application's bundled LAME source or
its binary signal assets. Recording signals are synthesized at run time, so no
third-party sound file is stored in this repository or in the RPM.
