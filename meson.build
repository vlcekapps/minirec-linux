project(
  'minirec-linux',
  version: '0.1.23',
  license: 'GPL-3.0-or-later',
  license_files: ['LICENSE', 'REPOSITORY_NOTICE.md'],
  meson_version: '>=1.2.0',
)

python = import('python').find_installation('python3')

runtime_check = run_command(
  python,
  '-c',
  '''
import sys


def release(value):
    numbers = []
    for component in value.split(".")[:2]:
        digits = ""
        for character in component:
            if not character.isdigit():
                break
            digits += character
        numbers.append(int(digits or 0))
    return tuple(numbers)


try:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")

    import gi
    if gi.version_info < (3, 48, 0):
        raise RuntimeError("PyGObject 3.48 or newer is required")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gst", "1.0")
    from gi.repository import Adw, Gst, Gtk

    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 20):
        raise RuntimeError("GTK 4.20 or newer is required")
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 7):
        raise RuntimeError("libadwaita 1.7 or newer is required")

    Gst.init(None)
    required = (
        "appsrc", "autoaudiosink", "audioconvert", "audioresample",
        "capsfilter", "fdsink", "valve", "volume", "opusenc", "oggmux",
        "lamemp3enc", "mpegaudioparse", "wavenc", "scaletempo",
    )
    missing = [name for name in required if Gst.ElementFactory.find(name) is None]
    if missing:
        raise RuntimeError("missing GStreamer elements: " + ", ".join(missing))
    if not (Gst.ElementFactory.find("pulsesrc") or Gst.ElementFactory.find("autoaudiosrc")):
        raise RuntimeError("a GStreamer audio input element is required")
    if not (Gst.ElementFactory.find("playbin3") or Gst.ElementFactory.find("playbin")):
        raise RuntimeError("a GStreamer playbin element is required")
except Exception as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)
''',
  check: false,
)
if runtime_check.returncode() != 0
  error('MiniRec runtime dependency check failed: ' + runtime_check.stderr().strip())
endif

python.install_sources(
  [
    'minirec/__init__.py',
    'minirec/__main__.py',
    'minirec/application.py',
    'minirec/gtk_helpers.py',
    'minirec/i18n.py',
    'minirec/models.py',
    'minirec/playback.py',
    'minirec/recording.py',
    'minirec/recovery.py',
    'minirec/settings.py',
    'minirec/storage.py',
    'minirec/ui.py',
  ],
  subdir: 'minirec',
)

install_subdir(
  'minirec/data',
  install_dir: join_paths(python.get_install_dir(), 'minirec'),
)

launcher_configuration = configuration_data()
launcher_configuration.set('PYTHON', python.full_path())
configured_bindir = get_option('bindir')
if not configured_bindir.startswith('/')
  configured_bindir = join_paths(get_option('prefix'), configured_bindir)
endif
launcher_configuration.set(
  'MINIREC_LAUNCHER',
  join_paths(configured_bindir, 'minirec'),
)
configure_file(
  input: 'data/minirec.in',
  output: 'minirec',
  configuration: launcher_configuration,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

configure_file(
  input: 'data/cz.pvlcek.minirec.desktop.in',
  output: 'cz.pvlcek.minirec.desktop',
  configuration: launcher_configuration,
  install: true,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)
install_data(
  'data/cz.pvlcek.minirec.metainfo.xml',
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)
install_data(
  'data/minirec.xml',
  install_dir: join_paths(get_option('datadir'), 'swcatalog', 'xml'),
)
install_data(
  'data/cz.pvlcek.minirec.svg',
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'scalable', 'apps'),
)
install_data(
  'LICENSE',
  'REPOSITORY_NOTICE.md',
  install_dir: join_paths(get_option('datadir'), 'licenses', 'minirec-linux'),
)
install_data(
  'README.md',
  'docs/android-parity.md',
  'docs/desktop-integration.md',
  install_dir: join_paths(get_option('datadir'), 'doc', 'minirec-linux'),
)
