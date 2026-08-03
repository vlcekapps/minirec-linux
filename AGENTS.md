# MiniRec Linux project rules

- The application is accessibility-first. Every user action must remain reachable by keyboard and Orca, without relying on icons, colour, pointer gestures, hover, sound, or context menus alone.
- Prefer standard GTK 4/libadwaita widgets and their native AT-SPI semantics. Any custom accessible role, state, name, relation, announcement, or focus change needs an automated test or a documented manual Orca check.
- Keep recording, recovery, storage, settings, timing, and playback policy independent of GTK wherever practical. Unit tests must be deterministic and offline; hardware microphone checks are separate, explicit smoke tests.
- Never overwrite an existing recording. Create recordings as private pending files, persist enough recovery state before capture starts, finalize with EOS, synchronize the result, and recover a verified safe prefix after interruption.
- Recordings belong to the user in the public `Recordings/MiniRec` directory. Package upgrades and application removal must not delete recordings or preferences.
- Recording may continue while the window is unfocused or hidden, but never after the application process exits. Closing during recording must require an explicit choice and safely finalize or keep the application open.
- Do not add autostart, a systemd service, or background operation after the app closes without an explicit user opt-in design.
- Do not change, build, install, or test the Android packages or connected ADB devices from this Linux repository.
- Every completed source, user-interface, packaging, bug-fix, or feature batch handed to the user must end with a newly built, upgradeable Fedora RPM containing the final changes. Bump the upstream version for a user-visible release; otherwise increment the RPM Release. Run unit, metadata, GUI, large-text, and accessibility gates first, then report the absolute RPM path and SHA-256.
