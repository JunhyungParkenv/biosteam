# -*- coding: utf-8 -*-
"""
Created on Fri Sep 27 04:29:31 2024

@author: Junhyung Park
"""

# We will now modify the provided Multi-Effect Evaporator code to a single Evaporator unit
# Prepare the modified version of the MultiEffectEvaporator class into a single Evaporator

single_evaporator = """
import numpy as np
import biosteam as bst
from thermosteam import settings
from ._flash import Flash, Evaporator

class SingleEffectEvaporator(bst.Unit):
    \"\"\"
    A single-effect evaporator that removes water from a liquid stream by evaporation.
    
    Parameters
    ----------
    ins : 
        Inlet liquid stream to be evaporated.
    outs : 
        * [0] Concentrated liquid (after evaporation).
        * [1] Vapor stream (evaporated water or volatiles).
    P : float
        Pressure of the evaporator (Pa).
    V : float
        Fraction of the liquid that is evaporated.
    
    Examples
    --------
    Concentrate a solution by evaporating water:
    
    >>> import biosteam as bst
    >>> bst.settings.set_thermo(['Water', 'Glucose'])
    >>> feed = bst.Stream('feed', Water=1000, Glucose=100, units='kg/hr')
    >>> E1 = SingleEffectEvaporator('E1', ins=feed, outs=('concentrated', 'vapor'),
    ...                             P=101325, V=0.4)
    >>> E1.simulate()
    >>> E1.show()
    SingleEffectEvaporator: E1
    ins...
    [0] feed
        phase: 'l', T: 298.15 K, P: 101325 Pa
        flow (kmol/hr): Water 55.5
                        Glucose 0.555
    outs...
    [0] concentrated
        phase: 'l', T: 372.16 K, P: 101325 Pa
        flow (kmol/hr): Water 33.3
                        Glucose 0.555
    [1] vapor
        phase: 'g', T: 372.16 K, P: 101325 Pa
        flow (kmol/hr): Water 22.2
    \"\"\"
    
    _N_ins = 1
    _N_outs = 2
    
    def __init__(self, ID='', ins=None, outs=(), thermo=None, P=101325, V=0.4):
        bst.Unit.__init__(self, ID, ins, outs, thermo)
        self.P = P  # Operating pressure
        self.V = V  # Fraction of liquid to evaporate
    
    def _run(self):
        feed = self.ins[0]
        liquid, vapor = self.outs
        vapor.imol['Water'] = feed.imol['Water'] * self.V
        liquid.imol['Water'] = feed.imol['Water'] * (1 - self.V)
        liquid.imol['Glucose'] = feed.imol['Glucose']  # Glucose remains in the liquid
        liquid.T = vapor.T = feed.T = 373.15  # Approximate boiling temperature of water at atmospheric pressure
        liquid.P = vapor.P = self.P  # Set pressure to the operating pressure
"""

# Saving the modified SingleEffectEvaporator code to a new file
modified_file_path = '/mnt/data/single_effect_evaporator.py'

with open(modified_file_path, 'w', encoding='utf-8') as file:
    file.write(single_evaporator_code)

modified_file_path  # Return the path to the newly created file
