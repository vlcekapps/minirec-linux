# MiniRec pro Linux

MiniRec je nativní a přístupný záznamník pro Linux. Port zachovává pracovní
postup Android verze 0.1.21, ale používá desktopové komponenty Python 3,
GTK 4, libadwaita, GStreamer a zvukovou vrstvu PipeWire/PulseAudio.

## Funkce

- jedna přehledná obrazovka ovladatelná klávesnicí a Orcou;
- čeština, angličtina a automatická volba podle systému;
- moderní Ogg/Opus jako výchozí formát, CBR MP3 a nekomprimované PCM16 WAV;
- kvality 32, 48, 64, 96, 128, 160, 192, 256 a 320 kb/s;
- skutečný stereofonní vstup s bezpečným přechodem na mono a přímá volba mono;
- digitální zesílení mikrofonu od −12 do +12 dB;
- Nahrát, Pozastavit, Pokračovat a Zastavit se zvukovými signály mimo nahrávku;
- odhad zbývajícího času z volného místa, formátu, kvality a počtu kanálů;
- veřejná složka `~/Recordings/MiniRec`, bezpečné názvy a obnova přerušených
  Ogg/Opus, MP3 a WAV souborů;
- procesní zámek proti souběžné recovery z jiné relace a hlídač zaseknutého EOS,
  který nikdy nemaže neověřený rozpracovaný soubor;
- přesný PCM bufferový limit, který ukončí klasický RIFF/WAV ještě před prvním
  celým bufferem, jenž by se nevešel do jeho 32bitových délek;
- journalované přejmenování a mazání přes ověřenou náhodnou karanténu, které při
  souběžné změně cesty skončí bezpečně bez zásahu do náhradního souboru;
- seznam nahrávek s datem, délkou, velikostí a explicitně uvedeným formátem,
  jednotlivé i hromadné mazání a otevření složky;
- interní přehrávač, posun o 10 sekund a rychlost 0,5× až 2× beze změny výšky;
- zachování přehrávání, pozice a rychlosti při přejmenování otevřené nahrávky;
- volitelná ochrana proti uspání po dobu aktivní či pozastavené nahrávky;
- desktopové oznámení s akcemi Pozastavit/Pokračovat a Zastavit, které po
  aktivaci vrátí uživatele do hlavního okna.

Androidová oprávnění, dotyková vrstva a ovládání tlačítky hlasitosti se na
desktop nepřenášejí. Androidí systémové sdílení rovněž nemá na Linuxu univerzální
protějšek; nahrávky jsou místo toho přímo dostupné ve veřejné složce a aplikace
umí tuto složku otevřít ve správci souborů.

## Spuštění na Fedoře

Používají se systémové balíčky, nikoli GTK nebo GStreamer instalované přes pip:

```bash
sudo dnf install \
  python3 python3-gobject gtk4 libadwaita \
  gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
  pipewire-gstreamer
cd /home/pvlcek/minirec
./run.sh
```

Vyžaduje Python 3.11+, PyGObject 3.48+, GTK 4.20+, libadwaitu 1.7+ a
GStreamer s dostupným vstupem, přehrávačem a enkodéry zvolených formátů.
MP3 používá systémový prvek `lamemp3enc`; MiniRec nepřibaluje vlastní LAME.

Pro testy bez fyzického mikrofonu lze veřejnou složku přesměrovat proměnnou
`MINIREC_RECORDINGS_DIR`. Nastavení se ukládá podle XDG do
`$XDG_CONFIG_HOME/minirec`, obnovovací stav do `$XDG_STATE_HOME/minirec`.

## Instalace

```bash
meson setup _build --prefix="$HOME/.local"
meson install -C _build
```

Meson nainstaluje spouštěč, desktopový soubor, ikonu, AppStream metadata a
pythonový modul. Nahrávky ani uživatelské nastavení nejsou součástí instalace a
při aktualizaci zůstávají beze změny.

## Testy

```bash
python3 -m unittest discover -s tests -v
python3 tools/validate_appstream_catalog.py
```

Jednotková sada je deterministická a nepoužívá mikrofon ani síť. V grafické
uživatelské relaci se navíc spouští `tools/gui_smoke.py`,
`tools/large_text_smoke.py` a `tools/accessibility_smoke.py`. Ruční průchod s
Orcou a klávesnicí je samostatný výslovný acceptance test. Skutečný
stereofonní záznam v Ogg/Opus na cílovém mikrofonu již ručně prošel s čistým
výsledkem. Samostatný mono režim a hardwarové záznamy ve formátech MP3 a WAV
zůstávají závěrečnými výslovnými acceptance testy pro konkrétní zvukový
hardware; nejsou součástí automatických release gateů.

Automatická kodeková část používá skutečné systémové GStreamer prvky, ale místo
mikrofonu syntetický mono/stereo zdroj a místo reproduktorů `fakesink`. Ověří tak
EOS, kontejnery a formátovou obnovu bez zachycování okolního zvuku. Zbývající
hardwarové testy mono režimu, MP3 a WAV se proto spouštějí samostatně a
výslovně.

## Fedora RPM

```bash
./tools/build-rpm.sh
```

Výsledné RPM, SRPM a normalizovaný zdrojový archiv jsou v `dist/rpm`. Skript
ověří shodu verzí, jednotkové testy, desktopový soubor a AppStream metadata ještě
před sestavením balíčku.

## Licence a zdrojový kód

Copyright © 2026 Pavel Vlček. MiniRec pro Linux je svobodný software vydaný pod
licencí `GPL-3.0-or-later`; licenční grant a úplné znění jsou v souborech
`REPOSITORY_NOTICE.md` a `LICENSE`. Veřejný kanonický zdrojový repozitář je
<https://github.com/vlcekapps/minirec-linux>.

Podrobný rozdíl proti Android verzi popisuje
[`docs/android-parity.md`](docs/android-parity.md).
