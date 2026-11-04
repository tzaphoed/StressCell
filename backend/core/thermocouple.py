"""Seebeck-Koeffizient Type E aus der Referenzfunktion NIST ITS-90 (SRD 60).

E(t) in mV, t in °C, Polynome gelten von -270 °C bis 0 °C und von 0 °C bis 1000 °C. Unter
etwa 20 K ist die Tabelle unsicher, und im Magnetfeld ändert sich die Thermokraft. Absolute
Temperaturamplituden dort nur mit eigener Kalibrierung ernst nehmen.
"""

from __future__ import annotations

import numpy as np

_BELOW = np.array([-0.346578420130e-28, -0.558273287210e-25, -0.396736195160e-22, -0.164147763550e-19,
                   -0.439794973910e-17, -0.803701236210e-15, -0.102876055340e-12, -0.932140586670e-11,
                   -0.594525830570e-09, -0.258001608430e-07, -0.779980486860e-06, 0.454109771240e-04,
                   0.586655087080e-01, 0.0])
_ABOVE = np.array([0.359608994810e-27, -0.143880417820e-23, 0.214892175690e-20, -0.125366004970e-17,
                   -0.191974955040e-15, 0.650244032700e-12, -0.330568966520e-09, 0.289084072120e-07,
                   0.450322755820e-04, 0.586655087100e-01, 0.0])
_DBELOW, _DABOVE = np.polyder(_BELOW), np.polyder(_ABOVE)


def seebeck_type_e(T: float) -> float:
    """dE/dT in V/K bei T in K."""
    t = float(T) - 273.15
    t = max(t, -270.0)
    return float(np.polyval(_DBELOW if t < 0 else _DABOVE, t)) * 1e-3
