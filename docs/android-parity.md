# Funkční parita s MiniRec pro Android

Tento dokument mapuje Android MiniRec 0.1.21 (revize
`6190a51c4194`, inventarizovaná 3. srpna 2026) na nativní variantu pro Fedora/GNOME.
Uživatelský výsledek zůstává stejný, zatímco mobilní systémová rozhraní jsou
nahrazena standardními desktopovými rozhraními.

## Matice funkcí

| Android 0.1.21 | Linuxová implementace | Stav |
| --- | --- | --- |
| Nahrát, Pozastavit, Pokračovat a Stop | Stejný stavový automat, samostatná viditelná tlačítka a GStreamer EOS finalizace | Zachováno |
| Čas nahrávky bez započtení pauz | Monotónní časovač aktivních úseků | Zachováno |
| Čtyři signální sekvence z WAV assetů | Stejné pořadí a mezery, tóny se syntetizují mimo záznam | Ekvivalent bez binárních assetů |
| AAC-LC v M4A jako výchozí | Ogg/Opus v `.oga` jako výchozí | Záměrná linuxová odchylka |
| CBR MP3 32–320 kb/s | Systémový GStreamer `lamemp3enc`, stejné volby | Zachováno |
| PCM16 WAV | GStreamer `wavenc`, mono/stereo a bufferový guard před přesným klasickým RIFF limitem | Zachováno |
| Stereo a automatický čistý mono fallback | Požadovaný počet vstupních kanálů PipeWire/PulseAudio a mono fallback | Zachováno |
| Androidí audit UNPROCESSED/NS/AGC/AEC a opt-in zpracované stereo | Není přeneseno; desktop používá výchozí systémový vstup bez Android audio-source režimů | Nerelevantní pro Linux |
| Digitální gain −12 až +12 dB | GStreamer `volume` před kodérem | Zachováno |
| Odhad zbývajícího času a 64 MiB rezerva | Volné místo, zvolený kodek, bitrate, kanály a WAV limit | Zachováno |
| `Recordings/MiniRec` nebo starší `Music/MiniRec` | Veřejná `~/Recordings/MiniRec`, testovatelná přes env override | Desktopový ekvivalent |
| Pending MediaStore řádek a recovery journal | Skrytý pending soubor, atomický XDG journal a ověření device/inode | Desktopový ekvivalent |
| Formátově bezpečná obnova M4A/MP3/WAV | Bezpečný prefix Ogg pages, CBR MP3 frames nebo zarovnaného WAV PCM | Zachováno pro linuxové formáty |
| Seznam nejnovějších nahrávek s datem, délkou, velikostí a formátem | Přístupný GTK seznam se stejným řazením; každý řádek explicitně uvádí datum, délku, velikost a formát | Zachováno |
| Přejmenování bez kopie a přepsání | Journalovaný Linux `renameat2(RENAME_NOREPLACE)`, zachovaná přípona a kontinuita otevřeného přehrávače včetně pozice a rychlosti | Zachováno |
| Jednotlivé a hromadné mazání, výběr max. 500 | Viditelné klávesnicové akce a dvoufázový journal s hidden karanténou | Zachováno |
| Android share sheet pro jednu či více nahrávek | Otevření veřejné složky ve správci souborů | Záměrná desktopová odchylka |
| Interní přehrávač ±10 s | GStreamer playbin, stejný posun a přístupný slider | Zachováno |
| Rychlosti 0,5×–2× bez změny výšky | GStreamer seek rate a `scaletempo` | Zachováno |
| Runtime oprávnění mikrofonu, médií a oznámení | Spravuje PipeWire, desktopový sandbox a GNOME; vlastní obrazovka oprávnění není | Nerelevantní pro Linux |
| Foreground service a wake lock | Záznam běží v procesu při minimalizaci; po ukončení procesu neběží | Desktopový ekvivalent |
| Ovládací oznámení | `GNotification` s Pause/Resume, Stop a návratem do hlavního okna; bez periodického elapsed resend kvůli Orce | Zachováno v mezích GNOME API |
| Nezamykat displej | Volitelný `Gtk.Application.inhibit` pro idle/suspend po celou relaci | Desktopový ekvivalent |
| Tlačítka hlasitosti a TalkBack dotyková pravidla | Neimplementováno podle zadání portu | Záměrně mimo rozsah |
| Čeština, angličtina a systémový jazyk | Stejné tři volby, rozhraní se po změně obnoví | Zachováno |
| Poděkovat autorovi | Stejný odkaz v hlavním menu | Zachováno |

## Hardwarová přejímka

Skutečný stereofonní záznam v Ogg/Opus na cílovém mikrofonu již ručně prošel s
čistým výsledkem. Samostatný mono režim a hardwarové záznamy ve formátech MP3 a
WAV zůstávají závěrečnými výslovnými acceptance testy; automatická sada
mikrofon neotevírá.

## Přístupnost

Rozhraní je auditováno proti lokální revizi Linux Accessibility Development
Guide `a477501d3f97ffa1465a81c4a86508ce9af4ff38`. Používá standardní GTK role,
pojmenované textové ovladače, nativní dropdown od GTK 4.20, čitelné stavy,
logický Tab order a dialogy s návratem fokusu. Žádná akce není dostupná pouze
myší, gestem, ikonou, barvou, zvukem nebo kontextovým menu.

Rutinní změny nahrávání se neoznamují opakovaným živým textem, aby hlas Orky
nevstoupil do mikrofonu. Stav však zůstává programově a při ručním procházení
čitelný; chyby, mono fallback, plné úložiště a obnovený soubor používají zdvořilé
oznámení. Úspěšné přejmenování a úplné či částečné smazání se rovněž jednou
oznámí; fokus se po změně vrátí na správný nebo nejbližší přeživší řádek.

## Bezpečnost dat

Linuxový port nikdy nepřepisuje existující nahrávku. Před otevřením cíle uloží
malý journal bez zvukových dat, synchronizuje jej a ověří identitu cíle pomocí
cesty, zařízení a inode. Normální Stop pošle do GStreamer pipeline EOS, počká na
uzavření kontejneru, zavolá `fsync` a atomicky zveřejní jméno výhradně přes
linuxové `renameat2(RENAME_NOREPLACE)`. Pokud syscall nebo tato jeho varianta
není podporována, operace skončí fail-closed bez hardlink fallbacku. Cílový
regular file se ještě jednou ověří podle device/inode, adresář se synchronizuje
a teprve potom se odstraní journal. Stejně journalované a ověřené je i
přejmenování už hotové nahrávky.

Po nestandardním ukončení se mění jen jednoznačně identifikovaný pending soubor.
Nejistý cíl se nemaže ani neopravuje. Delete journal předem a durable uloží
device/inode, původní cestu i 144bitové náhodné hidden jméno karantény na stejném
filesystemu. Teprve potom se přesný soubor přesune pomocí NOREPLACE, ověří,
synchronizuje a odstraní z karantény, nikoli z veřejné znovu použitelné cesty.
Nový proces destruktivní krok nikdy neopakuje: přesný inode nalezený v karanténě
se pokusí NOREPLACE vrátit na původní jméno; při konfliktu nebo změně identity
ponechá oba soubory i journal jako nejisté. Staré v1 delete journaly zůstávají
čitelné a vyhodnocují se pouze pozorováním.

Stejný předem journalovaný karanténní protokol používá Abort, úklid prázdného
pending souboru a kompatibilní úklid duplicity po dřívějším hardlink fallbacku.
Po pádu se audio raději obnoví pod pending jméno a oznámí jako nejisté, než aby
startup znovu provedl `unlink`. Otevřený descriptor, `O_NOFOLLOW`, další kontrola
identity těsně před `unlink` a nepředvídatelné jméno chrání proti běžné souběžné
výměně cesty. Absolutní obranu proti úmyslně škodícímu procesu stejného UID se
stejnými právy k adresáři samotné Linux pathname API zaručit nemůže; každá
detekovaná výměna proto končí fail-closed s ponechaným journalem.

Také každá zveřejněná položka knihovny nese device/inode z okamžiku načtení.
Přejmenování i mazání se odmítne, pokud byla pod stejnou cestu mezitím vložena
jiná nahrávka; částečný delete výsledek se nehlásí jako úplný úspěch.

Celý XDG state strom navíc po dobu běhu vlastní jediný proces prostřednictvím
neblokujícího advisory zámku. Dvě relace téhož uživatele proto nemohou zaměnit
živou nahrávku první instance za havarovaný soubor určený k obnově. Pokud
ovladač přijme EOS, ale nedoručí jej muxeru, hlídač ukončí I/O, synchronizuje
pending soubor a ponechá rozhodnutí formátově bezpečné obnově.
