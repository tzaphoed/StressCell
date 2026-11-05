"""Schnittstellen, die der Sequenzer von den Geräten erwartet.

Echte Treiber und Simulation implementieren dieselben Methoden. Sequenzer, Guard
und Regler kennen nur diese Klassen. Einheiten: V, A, V/s, pF, K, K/min, T, T/min.
Zeiten in Laborsekunden. Alle Lesemethoden werfen bei Gerätefehlern, der
Supervisor macht daraus None und behandelt den Wert als ungültig.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

CHANNELS = ("tension", "compression")


class PiezoSupply(ABC):
    """Zwei vierquadrantenfähige Kanäle: tension und compression."""

    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def set_output(self, ch: str, on: bool) -> None:
        """Aus heißt: Stapel liegen über einen Entladewiderstand kurz."""

    @abstractmethod
    def output(self, ch: str) -> bool: ...

    @abstractmethod
    def set_voltage(self, ch: str, volts: float) -> None:
        """Zielwert. Das Gerät fährt ihn mit der eingestellten Slew Rate an."""

    @abstractmethod
    def voltage_setpoint(self, ch: str) -> float: ...

    @abstractmethod
    def voltage_now(self, ch: str) -> float:
        """Momentanwert der Rampe."""

    @abstractmethod
    def current(self, ch: str) -> float: ...

    @abstractmethod
    def set_slew(self, ch: str, volts_per_s: float) -> None: ...

    @abstractmethod
    def slew(self, ch: str) -> float: ...

    def close(self) -> None:
        pass


class CapacitanceMeter(ABC):
    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def configure(self, frequency_Hz: float, excitation_Vrms: float) -> None: ...

    @abstractmethod
    def read(self) -> dict:
        """{"C_pF", "loss", "V_exc"}. C_pF ist None, wenn das Gerät keinen Wert liefert."""

    def close(self) -> None:
        pass


class Cryostat(ABC):
    """PPMS oder Kryostat vergleichbarer Art."""

    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def read(self) -> dict:
        """{"T", "T_status", "B", "B_status", "chamber"}"""

    @abstractmethod
    def set_temperature(self, T: float, rate_K_min: float, mode: str = "fast_settle") -> None: ...

    @abstractmethod
    def set_field(self, B: float, rate_T_min: float, mode: str = "linear",
                  persistent: bool = False) -> None: ...

    @abstractmethod
    def set_chamber(self, mode: str) -> None:
        """seal | purge_seal | vent_seal | pump_continuous | vent_continuous | high_vacuum"""

    def close(self) -> None:
        pass


class NoCryostat(Cryostat):
    """Kein PPMS: keine Temperatur, kein Feld, keine Kammer. Die Software rechnet dann mit dem
    engsten Fenster (300 K und unbekannte Kammer) und lehnt jede Steuerung des Kryostaten ab."""

    def identify(self):
        return "no cryostat (PPMS data switched off)"

    def read(self):
        return {"T": None, "T_status": None, "B": None, "B_status": None, "chamber": None}

    def set_temperature(self, T, rate_K_min, mode="fast_settle"):
        raise IOError("PPMS is switched off in the settings")

    def set_field(self, B, rate_T_min, mode="linear", persistent=False):
        raise IOError("PPMS is switched off in the settings")

    def set_chamber(self, mode):
        raise IOError("PPMS is switched off in the settings")


class SampleMeter(ABC):
    """Optionale Messgröße an der Probe, z.B. Widerstand über die WP101-Plattform."""

    @abstractmethod
    def read(self) -> dict:
        """Beliebige Spalten, z.B. {"R_ohm": ...}"""

    def close(self) -> None:
        pass


class LockIn(ABC):
    """Referenz für die AC-Anregung und Messung der Thermospannung.

    Amplituden auf dieser Schnittstelle sind die Spitzenspannung am Stapel. Die Umrechnung
    auf den Geräteausgang steckt im Treiber, wegen der Verstärkung der Kette."""

    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def set_drive(self, frequency_Hz: float, V_stack_peak: float) -> None:
        """V_stack_peak = 0 schaltet die Anregung aus."""

    @abstractmethod
    def set_detection(self, harmonic: int, time_constant_s: float) -> None: ...

    @abstractmethod
    def read(self) -> dict:
        """{"X", "Y", "R", "theta", "f"} in V rms am Eingang, Grad, Hz."""

    def close(self) -> None:
        pass


class Instruments:
    def __init__(self, supply: PiezoSupply, cap: CapacitanceMeter, cryo: Cryostat,
                 sample: SampleMeter | None = None, closers=(), lockin: LockIn | None = None):
        self.supply = supply
        self.cap = cap
        self.cryo = cryo
        self.sample = sample
        self.lockin = lockin
        self._closers = tuple(closers)

    def close(self):
        for dev in (self.lockin, self.supply, self.cap, self.cryo, self.sample):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
        for fn in self._closers:
            try:
                fn()
            except Exception:
                pass
