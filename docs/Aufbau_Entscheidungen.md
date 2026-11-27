# Unser Aufbau: Geräteauswahl und Entscheidungen

Dieses Dokument hält fest, für welche Geräte und welchen Aufbau wir uns entschieden haben, und warum.
Das Nachschlagewerk mit allen Alternativen ist [`FC100_Geraeteauswahl.pdf`](FC100_Geraeteauswahl.pdf).
Unser Aufbau entspricht dort **Aufbau B („alles SR830“)**, mit AH 2700A für die Kraft und
Lake Shore 370 für das Zellthermometer. Die passenden Werte stehen in `backend/config.toml`.

## Ziel der Messung

- **Probe:** Heusler (NiMnSn), **polykristallin**, in der Razorbill FC100 im PPMS
- **Temperaturbereich:** 2 bis 250 K
- **Übergänge:** martensitisch (Martensit nach Austenit, erster Ordnung, hysteretisch) und Tc des Austenits (zweiter Ordnung)
- **Messungen:**
  - T-Sweeps bei festem Feld und fester Kraft, das PPMS fährt T und Rate
  - Stress-Sweeps bei ausgewählten T und H
  - AC-Elastokalorik bei f, dazu das 2f-Signal
- **Endziel:** Entropie S(T, H, σ), siehe Abschnitt „Entropie“

## Geräte

| Funktion | Gerät | Status | Eintrag in `config.toml` |
|---|---|---|---|
| DC-Offset auf den **Druckstapel** | Keithley 2400, davor R1 = 15 kΩ, Bleeder 1 MΩ parallel zum Stapel | vorhanden | `[instruments.compression]` `model = "keithley2400"` |
| AC-Anregung auf den **Zugstapel** | SR830 SINE OUT, davor **R2 = 680 Ω**, Bleeder 1 MΩ | vorhanden | `lockin_model = "sr830"`, `[ac] series_R_ohm = 730` |
| Thermospannung (ΔT bei f) | Thermoelement Type E, 12,5 oder 25 µm, differentiell → **SR560** (Batterie, A−B) → derselbe SR830 | SR560 beschaffen oder ausleihen | `[ac] preamp_gain` = Verstärkung am SR560 |
| Kraft (DC) | AH 2700A, Messfrequenz weg von der ECE-Frequenz | vorhanden | `cap = "ah2700a"`, `capmeter` |
| Zellthermometer | Cernox CX-1050, **kalibriert** gekauft, auf der Thermal Link Plate nahe der Referenzlötstelle | Cernox beschaffen | `[cell_thermometer]` |
| Auslesen Cernox | Lake Shore 370 mit Scanner, Cernox auf einem Kanal | vorhanden | `model = "370"`, `input` = Kanal am Scanner |
| Kryostat | PPMS (T, Rate, Feld), StressCell liest über MultiPyVu | vorhanden | `cryo = "multipyvu"` |
| Kontrolle | Oszilloskop: Spannung am Zugstapel hinter R2, Kurvenform, `gain_verified` | im Labor | von Hand |
| Reserve | Keithley 2410 (die Software klemmt auf den 200-V-Bereich), Keithley 2450 (nur im SCPI-Modus, nicht TSP) | vorhanden | |
| Wärmekapazität | PPMS-Option Heat Capacity | vorhanden | außerhalb StressCell |
| Gegenprobe ΔS_t | DSC, M(T, H) am VSM oder MPMS | im Haus prüfen | außerhalb StressCell |
| Optional: 2f gleichzeitig | zweiter SR830 an TTL REF des ersten, Harmonic = 2 | optional | `lockin2`, `lockin2_model`, `lockin2_harmonic = 2` |

**Bewusst nicht gewählt:**
- **Hochspannungsverstärker für die AC-Anregung.** Für NiMnSn reichen 0,3 bis 2 V Spitze am Stapel, und die Stromgrenze der Stapel liegt bei 10 mA. Ein Verstärker würde nur das Risiko erhöhen.
- **Keysight E4980A.** Die AH 2700A ist genauer, und ihre Messfrequenz lässt sich von der ECE-Frequenz wegschieben.

## Aufbau und Verkabelung

- **Stapelgruppen** laut Datenblatt, Tabelle 1:
  - Zugstapel: rot (+), weiß (−). Darauf kommt die **AC-Anregung**.
  - Druckstapel: braun (+), blau (−). Darauf kommt der **DC-Offset**.
  - AC auf beiden Gruppen gleichphasig würde sich aufheben. Vorzeichen: D = V_T − V_C, D > 0 ist Zug.
- **Anschlussbox HV:** R1 vor dem Keithley, R2 vor dem SR830, je ein Bleeder parallel zu jeder Stapelgruppe (mindestens 500 V Spannungsfestigkeit). Bauteilliste in der Geräteauswahl, Seite 9.
  - Die Bleeder entladen die Stapel dauerhaft, die Stapel hängen also nie offen. Das schützt gegen die pyroelektrische Spannung beim Abkühlen und Aufwärmen.
  - Erst wenn sie eingebaut sind, `bleeder_fitted = true` setzen.
- **Signalbox Thermoelement:**
  - Ein zweipoliger Schalter mit den Stellungen „Messen“ und „Kurz“. Beide Adern schalten immer gemeinsam, sonst hängt das Thermoelement halb offen.
  - Innen Kupfer, verdrillt und symmetrisch gelötet. Das hält die Thermo-EMKs gleich, sie heben sich auf.
  - Vor jedem Umstecken auf „Kurz“ stellen (Schutz gegen elektrostatische Entladung).
  - Hochspannung und nV-Signal nicht in dasselbe Gehäuse.
- **Kraftsensor:** Koax direkt zur AH, ohne Schalter dazwischen.
  - Gelbes Band an High, schwarzes an Low.
  - Die Schirme dürfen den Kryostaten nicht berühren.
- **Thermoelement:**
  - Lötstelle 1 auf der Probe, **elektrisch isoliert** mit einer dünnen Schicht GE-Lack oder Stycast. Die Probe ist metallisch, ohne Isolierung entsteht eine Masseschleife über die Zelle.
  - Lötstelle 2 auf der Thermal Link Plate, neben dem Cernox.
  - Die Schleife klein halten und verdrillen. Im Feld induziert der mitschwingende Draht sonst eine Spannung bei f, die in Quadratur liegt.
- **Masse:** ein Sternpunkt. Die Masseklemme des Oszilloskop-Tastkopfs nur an die Lock-in-Seite (Minuspol Zugstapel) legen, nie an die Keithley-Seite.

## Frequenz und 2f

- **Frequenz-Sweep** (Seite „Messen“ › Frequenzsweep) bei mehreren T: tief, hoch, nahe dem Übergang. Gesucht ist das Plateau, in dem die Amplitude flach und die Phase konstant ist.
- **f so legen, dass f und 2f beide im Plateau liegen**, also in die untere Hälfte des Plateaus.
- **Abstand zur AH-Brückenfrequenz** und ihren Vielfachen halten. Die Software lehnt Frequenzen innerhalb ±3 % ab.
- **RC-Tiefpass:** Mit R2 = 680 Ω liegt die Ecke bei etwa 70 Hz, also mitten im erwarteten Plateau. Die Software rechnet sie heraus. Die Spannung am Stapel trotzdem mit dem Oszilloskop prüfen, weil C mit T fällt.
- **Was 2f zeigt:**
  - die Linearität des 1f-Signals
  - die Krümmung: 2f ≈ (s/4)·dA₁/dσ
  - **Hysterese bzw. Dissipation** an der martensitischen Umwandlung, mit fester Phase und eigenem Amplitudenverhalten
  - Artefakte (Verzerrung, Erwärmung des Piezos) und rutschenden Kleber bzw. Risse, erkennbar daran, dass 2f bei festen Bedingungen über die Zeit wächst
- **AC-Spannung σ_ac** kommt aus G = dF/dD, das die Software quasistatisch misst (`measure_gain`), und der Spannung am Stapel.

## Entropie S(T, H, σ)

AC-Elastokalorik allein liefert keine Entropie. Unser Weg:

1. **S(T, H, 0) absolut:** C_p(T) bei mehreren Feldern messen, S = ∫ C/T dT.
   - Am Übergang erster Ordnung die **Langpuls-Auswertung** nutzen (Lashley et al., Cryogenics 43, 369, 2003). Die 2τ-Standardauswertung verliert dort die latente Wärme.
2. **Außerhalb der Übergänge: Adiabatenverfahren.** Entlang einer Adiabate bleibt S gleich, ihre Steigung ist die gemessene
   g = (∂T/∂σ)_S. Also S(T, σ) = S(T₀, 0) mit T₀ am Ende der Adiabate bei σ = 0. Das braucht keine Wärmekapazität unter
   Spannung, anders als der Weg über (∂S/∂σ)_T = −(C/T)·g mit C bei σ = 0, der nur eine Näherung ist.
   Werkzeug: `tools/entropy_map.py` (Handbuch, Kapitel „Entropie“). g mit Vorzeichen (`dT_dsigma_signed_K_MPa`),
   Polung einmal mit `[ac] tc_sign` am thermoelastischen Effekt festlegen: Zug kühlt weit weg vom Übergang.
3. **Austenit-Tc** (zweite Ordnung): Dort ist die AC-Methode am stärksten. dTc/dσ kommt aus der Peaklage.
4. **Martensitischer Übergang:**
   - ΔS_t aus C_p bzw. DSC, die Verschiebung dT_t/dσ aus den T-Sweeps bei mehreren Kräften.
   - Daraus folgt die Entropiekurve unter Stress als starre Verschiebung. Die Annahme explizit nennen: Sie gilt, solange sich ΔS_t und die Form des Übergangs über den σ-Bereich kaum ändern.
   - Die AC-Methode sieht hier nur reversible Unterschleifen. Für ΔS_t bitte nicht AC-Elastokalorik nehmen.
5. **Konsistenz:** ΔS_t aus C_p, aus DSC, aus dem magnetischen Clausius-Clapeyron mit M(T, H) und aus dem Verhältnis von feld- zu stressinduzierter Verschiebung von T_t müssen zusammenpassen.

**Warum die fehlende Dehnungsmessung beim Polykristall wenig schadet:** Im spannungsfreien Polykristall akkommodieren sich die Martensitvarianten gegenseitig. Δε_t ist deshalb bei kleinem σ ohnehin schlecht definiert. ΔS_t verankern wir auf C_p bzw. DSC, nicht auf Δε_t.

**Risiken beim Polykristall:**
- Probenbruch bzw. Risse an Korngrenzen bei jedem Zyklus durch die Umwandlung. Reproduzierbarkeit prüfen und Ersatzproben einplanen.
- Bei großen Körnern im Verhältnis zum Probenquerschnitt misst man effektiv nur wenige Körner. Korngröße angeben und mindestens zwei Proben messen.

## Korrekturen gegenüber dem ersten Entwurf

Ein Teil der Entscheidungen fiel in einem Gespräch, bevor StressCell und das Datenblatt einbezogen waren.
Dort weicht das Gespräch ab, gilt dieses Dokument:

| Erster Entwurf | Gilt jetzt | Grund |
|---|---|---|
| Hochspannungsverstärker für AC | kein Verstärker, SR830 direkt über R2 | 0,3 bis 2 V Spitze reichen, Stromgrenze 10 mA |
| „innerer“ und „äußerer“ Stapel | Zug- und Druckstapel, Farben laut Datenblatt | Bezeichnung der FC100 |
| HV-Kurzschlussbox mit 100 MΩ und 10 kΩ zum Entladen | R1 = 15 kΩ und Bleeder 1 MΩ dauerhaft in der Anschlussbox | wie im RP100, die Stapel hängen nie offen |
| lineare Kraftkalibrierung F = k(T)·(C − C0) | Razorbill-Formel F = κ(T)·(α/(C − Cp) − f0(T)) | Kalibrierblatt der Zelle #0260 |
| Lake Shore 372 über `KRDG?` | `RDGK?` und `RDGR?` | Befehlssatz des 370 und 372, so im Treiber umgesetzt |
| T nur vom Cernox, Feld von Hand | Cernox über den 372 **und** PPMS über MultiPyVu | Das Spannungsfenster nimmt die wärmere der beiden Temperaturen, das Feld kommt automatisch |

## Offene Punkte vor dem ersten Einsatz

- [ ] Kalibrierblatt prüfen, in `calibrations/FC100_0260.toml` `verified = true` setzen
- [ ] R2 = 680 Ω, R1 = 15 kΩ und beide Bleeder einbauen, danach `bleeder_fitted = true`
- [ ] Verstärkung der AC-Kette mit dem Oszilloskop am Stapel messen, danach `gain_verified = true`
- [ ] Lake Shore 370: Kurve laden und zuweisen (`python tools/lakeshore_curve.py calibrations/cernox.340 --address ... --curve 1 --channel 1 --write`), Autoscan aus, kleine Anregung einstellen (Eigenerwärmung bei 2 K prüfen), `input` in der Config eintragen
- [ ] Verstärkung am SR560 = `preamp_gain`
- [ ] Messfrequenz der AH festlegen, mit Abstand zur ECE-Frequenz und zu 2f
- [ ] Thermokraft des Type E im Feld: je Feld relativ auswerten oder kalibrieren
- [ ] Erster Kontakt: `check_instruments.py` mit `read_only = true`
- [ ] 2f gleichzeitig: zweiten SR830 anschließen (TTL des ersten an REF IN), `lockin2` eintragen
- [ ] `[ac] tc_sign` am thermoelastischen Effekt prüfen: weit weg vom Übergang muss Zug kühlen
- [ ] Volles ΔT_ad bei großen Spannungsschritten: Oszilloskop oder Nanovoltmeter-Puffer, der Messtakt von StressCell (1 s) ist dafür zu langsam
