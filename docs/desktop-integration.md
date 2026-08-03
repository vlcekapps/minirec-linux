# Desktop audio, notifications and lifecycle

MiniRec používá veřejná rozhraní Fedory a GNOME a každá volitelná integrace je
fail-safe. Chybějící session D-Bus nebo desktopová oznámení nesmějí zabránit
nahrávání ani bezpečnému dokončení souboru.

## Audio backend

Záznam používá GStreamer nad PulseAudio kompatibilní vrstvou PipeWire. Pipeline
záměrně vyžaduje počet kanálů na vstupu ještě před konverzí, aby volba stereo
neznamenala pouhé zduplikování mono kanálu. Když zdroj stereo nevyjedná, backend
zkusí mono a tento fallback předá UI jako čitelnou událost.

Formáty jsou sestavené ze systémových prvků:

- `opusenc ! oggmux` pro Ogg/Opus;
- `lamemp3enc target=bitrate cbr=true` pro MP3;
- `wavenc ! audio/x-wav` pro klasický RIFF/PCM16 WAV.

Gain zajišťuje `volume` před kodérem. Pauza pozastaví capture pipeline a časovač,
ale zachová kodér i soubor. Stop používá EOS; tvrdé přepnutí pipeline do NULL bez
EOS není považováno za úspěšné dokončení.

U WAV hlídá pad probe přímo před `wavenc` každý přijatý PCM buffer. Propustí
jen celé zvukové rámce, které se vejdou do obou 32bitových polí klasického
RIFF, a před prvním přetékajícím bufferem zavře capture gate a odloží běžné
EOS dokončení na hlavní GLib kontext. Platnost WAV proto nezávisí na periodě
GUI časovače ani na dostupném místě na disku.

Přijatý EOS má konečný časový limit. Pokud jej vadný zdroj nebo plugin nikdy
nepropustí k muxeru, MiniRec synchronně zastaví I/O a ponechá přesně
identifikovaný pending soubor i journal. Následující obnova pak zveřejní pouze
ověřený Ogg page, MP3 frame nebo zarovnaný WAV prefix.

Přehrávač používá `playbin3`, případně `playbin`, a filtr `scaletempo`. Změna
rychlosti proto nemění výšku hlasu. Zavření přehrávače jej vždy zastaví; pouhá
ztráta fokusu nikoli. Po dokončení přípravy drží playbin otevřený inode, takže
přejmenování pouze přemapuje cestu a viditelný název bez ztráty fáze, pozice nebo
rychlosti. Dokud je médium ve stavu preparing, kontrolér odmítne rename/delete
téže položky z přehrávače i z paralelně otevřeného seznamu.

## Životní cyklus

Minimalizace a přepnutí do jiné aplikace aktivní záznam nepozastaví. MiniRec ale
neběží po ukončení procesu a neinstaluje systemd službu ani autostart. Pokus o
zavření okna během relace otevře přístupné potvrzení; uživatel může zůstat v
aplikaci nebo nahrávku bezpečně zastavit a dokončit.

Volba „Zabránit uspání během nahrávání“ používá aplikační inhibit cookie po dobu
stavů preparing, recording, paused a stopping. Cookie se uvolní při dokončení,
chybě i shutdownu.

XDG state adresář je po celý život procesu chráněn linuxovým advisory zámkem.
Ten doplňuje D-Bus single-instance chování i mezi dvěma grafickými relacemi.
Není-li možné zámek bezpečně získat, druhá instance nespustí recovery ani žádnou
zápisovou operaci a zobrazí textovou přístupnou chybu.

Příprava pending souboru, skenování knihovny, startup recovery, rename, delete
a publikace po EOS běží mimo GTK vlákno;
návrat do rozhraní používá generační token proti zastaralým výsledkům. Doba
trvání nezměněných souborů se cachuje podle device/inode/size/mtime, takže
obnovení seznamu znovu neprochází celé dlouhé Ogg nebo MP3 nahrávky.

## Oznámení

Aktivní záznam lze publikovat jako stabilní `Gio.Notification`. Oznámení má podle
stavu akci Pozastavit nebo Pokračovat a vždy akci Zastavit. Znovupoužití stejného
ID aktualizuje logickou notifikaci; dokončení ji stáhne. Výchozí aktivace
oznámení prezentuje hlavní okno. MiniRec neposílá periodické elapsed aktualizace:
GNOME ani Orca negarantují, že opakované nahrazení stejného ID bude tiché, a hlas
čtečky obrazovky nesmí být opakovaně vyvolán do probíhajícího záznamu.

## Spouštění z GNOME

Desktopový příkaz začíná stabilním systémovým interpretem a jako druhý
argument dostane absolutní cestu ke spouštěči MiniRec ze stejného instalačního
prefixu. V RPM jsou tyto cesty `/usr/bin/python3` a `/usr/bin/minirec`. GIO při
obnově seznamu aplikací ověřuje dostupnost prvního programu z `Exec`. Při
aktualizaci proto záznam zůstane platný i v okamžiku, kdy transakce nahrazuje
balíčkem vlastněný soubor `/usr/bin/minirec`. Desktopovou a ikonovou cache po
transakci obnovují standardní Fedora file triggery.

`GNotification` neposkytuje Androidí foreground-service garanci, vlastní stav
zamčené obrazovky ani právo vynutit zobrazení tlačítek. GNOME Shell a uživatelská
nastavení mají poslední slovo. Hlavní okno proto vždy obsahuje stejné viditelné
ovládání a oznámení není jediná cesta k žádné funkci.

## Soubory a správce souborů

Výchozí nahrávky jsou v `~/Recordings/MiniRec`. Aplikace vytváří jen svůj
podadresář, nemění jiné uživatelské soubory a při odinstalování data nemaže.
Akce „Otevřít složku nahrávek“ předá její `file://` URI výchozímu správci
souborů pomocí `Gio.AppInfo.launch_default_for_uri`.

Publikace a přejmenování používají výhradně linuxové
`renameat2(RENAME_NOREPLACE)` a po přesunu znovu ověří device/inode. Mazání,
Abort a úklid pending souboru nejdřív durable zapíší původní i náhodnou skrytou
karanténní cestu na stejném filesystemu. Startup starý destruktivní krok nikdy
neopakuje: přesný nalezený inode pouze bezpečně vrátí, nebo ponechá operaci jako
nejistou. Nepodporovaný `renameat2` proto znamená fail-closed, ne přepis či
hard-link fallback.

Linux nemá univerzální share sheet s kontraktem `SEND`/`SEND_MULTIPLE`. Přímý
přístup k originálům a otevření složky jsou proto předvídatelnější než vlastní
neúplný seznam cílových aplikací.

## Release gates

Před RPM sestavením proběhnou offline jednotkové testy, strict AppStream
validace a kontrola desktopového souboru. V grafické relaci se navíc ověřuje:

- celé rozhraní pouze klávesnicí;
- AT-SPI role, názvy, stavy, hodnoty a focus return;
- angličtina i čeština při 22pt písmu a šířce 320 px;
- High Contrast a tmavé systémové téma.

Skutečný stereofonní záznam v Ogg/Opus na cílovém mikrofonu již ručně prošel s
čistým výsledkem. Samostatný mono režim a hardwarové záznamy ve formátech MP3 a
WAV zůstávají závěrečnými výslovně spuštěnými acceptance testy. Automatická
sada mikrofon neotevírá; reálné kodeky, EOS, seek a recovery ověřuje
deterministicky přes syntetický zdroj a `fakesink`.
