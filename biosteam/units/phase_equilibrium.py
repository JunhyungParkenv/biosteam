# -*- coding: utf-8 -*-
# BioSTEAM: The Biorefinery Simulation and Techno-Economic Analysis Modules
# Copyright (C) 2020-2023, Yoel Cortes-Pena <yoelcortes@gmail.com>
# 
# This module is under the UIUC open-source license. See 
# github.com/BioSTEAMDevelopmentGroup/biosteam/blob/master/LICENSE.txt
# for license details.
"""
This module contains abstract classes for modeling separations in unit operations.

"""
from warnings import warn
from numba import njit, objmode
import thermosteam as tmo
from thermosteam import separations as sep
import biosteam as bst
import flexsolve as flx
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from math import inf
from typing import Callable
from scipy.optimize import root
from ..exceptions import Converged
from .. import Unit

__all__ = (
    'StageEquilibrium',
    'MultiStageEquilibrium',
)

# %% Equilibrium objects.

@njit(cache=True)
def _vle_phi_K(vapor, liquid):
    F_vapor = vapor.sum()
    F_liquid = liquid.sum()
    phi = F_vapor / (F_vapor + F_liquid)
    y = vapor / F_vapor
    x = liquid / F_liquid
    return phi, y / x 

def _get_specification(name, value):
    if name == 'Duty':
        B = None
        Q = value
    elif name == 'Reflux':
        B = inf if value == 0 else 1 / value
        Q = None
    elif name == 'Boilup':
        B = value
        Q = None
    else:
        raise RuntimeError(f"specification '{name}' not implemented for stage")
    return B, Q


class StageEquilibrium(Unit):
    _N_ins = 0
    _N_outs = 2
    _ins_size_is_fixed = False
    _outs_size_is_fixed = False
    auxiliary_unit_names = ('partition', 'mixer', 'splitters')
    
    def __init__(self, ID='', ins=None, outs=(), thermo=None, *, 
            phases, partition_data=None, top_split=0, bottom_split=0,
            B=None, Q=None,
        ):
        self._N_outs = 2 + int(top_split) + int(bottom_split)
        Unit.__init__(self, ID, ins, outs, thermo)
        mixer = self.auxiliary(
            'mixer', bst.Mixer, ins=self.ins, 
        )
        mixer.outs[0].phases = phases
        partition = self.auxiliary(
            'partition', PhasePartition, ins=mixer-0, phases=phases,
            partition_data=partition_data, 
            outs=(
                bst.Stream(None) if top_split else self.outs[0],
                bst.Stream(None) if bottom_split else self.outs[1],
            ),
        )
        self.top_split = top_split
        self.bottom_split = bottom_split
        self.splitters = []
        if top_split:
            self.auxiliary(
                'splitters', bst.Splitter, 
                partition-0, [self.outs[2], self.outs[0]],
                split=top_split,
            )
        if bottom_split:
            self.auxiliary(
                'splitters', bst.Splitter, 
                partition-1, [self.outs[-1], self.outs[1]],
                split=bottom_split, 
            )
        self.set_specification(B, Q)
    
    @property
    def Q(self):
        return self.partition.Q
    @Q.setter
    def Q(self, Q):
        self.partition.Q = Q
    
    @property
    def B(self):
        return self.partition.B
    @B.setter
    def B(self, B):
        self.partition.B = B
    
    @property
    def B_specification(self):
        return self.partition.B_specification
    @B_specification.setter
    def B_specification(self, B_specification):
        self.partition.B_specification = B_specification
    
    @property
    def T(self):
        return self.partition.T
    @T.setter
    def T(self, T):
        self.partition.T = T
        for i in self.partition.outs: i.T = T
    
    @property
    def K(self):
        return self.partition.K
    @K.setter
    def K(self, K):
        self.partition.K = K
        for i in self.partition.outs: i.K = K
    
    def _solve_decoupled_variables(self):
        partition = self.partition
        chemicals = self.chemicals
        phases = partition.phases 
        IDs = chemicals.IDs
        IDs_old = partition.IDs
        if IDs_old != IDs:
            K = np.ones(chemicals.size)
            for ID, value in zip(IDs_old, partition.K):
                K[IDs.index(ID)] = value
            partition.K = K
            partition.IDs = IDs
        if phases == ('g', 'l'):
            partition._run(update=False, couple_energy_balance=False)
            self._decoupled_variables = {'K', 'T'}
            if self.B_specification: self._decoupled_variables.add('B')
        elif phases == ('L', 'l'):
            partition._run(update=False, couple_energy_balance=False)
            self._decoupled_variables = {'K', 'B'}
        else:
            raise NotImplementedError(f'decoupled variables for phases {phases} is not yet implemented')
    
    def _create_temperature_departure_equation(self):
        # C1dT1 - Cv2*dT2 - Cl0*dT0 = Q1 - H_out + H_in
        coeff = {self: sum([i.C for i in self.outs])}
        for i in self.ins:
            source = self.owner.system._get_source_stage(i)
            if not source or 'T' in source._decoupled_variables: continue
            coeff[source] = -i.C
        return coeff, (self.Q or 0.) + self.H_in - self.H_out
    
    def _create_phase_ratio_departure_equation(self):
        # hV1*L1*dB1 - hv2*L2*dB2 = Q1 + H_in - H_out
        vapor, liquid = self.partition.outs
        coeff = {}
        if vapor.isempty():
            liquid.phase = 'g'
            coeff[self] = liquid.H
            liquid.phase = 'l'
        else:
            coeff[self] = vapor.h * liquid.F_mol
        for i in self.ins:
            if i.phase != 'g': continue
            source = self.owner.system._get_source_stage(i)
            if not source or 'B' in source._decoupled_variables: continue
            vapor, liquid = source.partition.outs
            if vapor.isempty():
                liquid.phase = 'g'
                coeff[source] = liquid.H
                liquid.phase = 'l'
            else:
                coeff[source] = -vapor.h * liquid.F_mol
        return coeff, (self.Q or 0.) + self.H_in - self.H_out
    
    def _create_mass_balance_equations(self):
        top_split = self.top_split
        bottom_split = self.bottom_split
        inlets = self.ins
        fresh_inlets = [i for i in inlets if i.isfeed()]
        process_inlets = [i for i in inlets if not i.isfeed()]
        top, bottom, *_ = self.outs
        top_side_draw = self.top_side_draw
        bottom_side_draw = self.bottom_side_draw
        equations = []
        ones = np.ones(self.chemicals.size)
        minus_ones = -ones
        zeros = np.zeros(self.chemicals.size)
        
        # Overall flows
        eq_overall = {}
        S = self.K * self.B
        for i in self.outs: eq_overall[i] = ones
        for i in process_inlets: eq_overall[i] = minus_ones
        equations.append(
            (eq_overall, sum([i.mol for i in fresh_inlets], zeros))
        )
        
        # Top to bottom flows
        eq_outs = {}
        eq_outs[top] = ones
        eq_outs[bottom] = -S
        equations.append(
            (eq_outs, zeros)
        )
        
        # Top split flows
        if top_side_draw:
            eq_top_split = {
                top_side_draw: top_split * ones,
                top: top_split + minus_ones,
            }
            equations.append(
                (eq_top_split, zeros)
            )
        
        # Bottom split flows
        if bottom_side_draw:
            eq_bottom_split = {
                bottom_side_draw: bottom_split * ones,
                bottom: bottom_split + minus_ones,
            }
            equations.append(
                (eq_bottom_split, zeros)
            )
        
        return equations
    
    def _create_linear_equations(self, variable):
        # list[dict[Unit|Stream, float]]
        if variable in self._decoupled_variables:
            eqs = []
        elif variable == 'T':
            eqs = [self._create_temperature_departure_equation()]
        elif variable == 'B':
            eqs = [self._create_phase_ratio_departure_equation()]
        elif variable == 'mol':
            eqs = self._create_mass_balance_equations()
        else:
            eqs = []
        return eqs
    
    def _update_decoupled_variable(self, variable, value):
        if variable == 'T':
            self.T = T = self.T + value
            self._decoupled_variables.add('T')
            for i in self.outs: i.T = T
        elif variable == 'B':
            self.B += value
            self._decoupled_variables.add('B')
    
    def add_feed(self, stream):
        self.ins.append(stream)
        self.mixer.ins.append(
            self.auxlet(
                stream
            )
        )
        
    def set_specification(self, B, Q):
        if B is None and Q is None: Q = 0.
        partition = self.partition
        partition.B_specification = partition.B = B
        partition.Q = Q
    
    @property
    def extract(self):
        return self.outs[0]
    @property
    def raffinate(self):
        return self.outs[1]
    @property
    def extract_side_draw(self):
        if self.top_split: return self.outs[2]
    @property
    def raffinate_side_draw(self):
        if self.bottom_split: return self.outs[-1]
    
    @property
    def vapor(self):
        return self.outs[0]
    @property
    def liquid(self):
        return self.outs[1]
    @property
    def vapor_side_draw(self):
        if self.top_split: return self.outs[2]
    @property
    def liquid_side_draw(self):
        if self.bottom_split: return self.outs[-1]
    @property
    def top_side_draw(self):
        if self.top_split: return self.outs[2]
    @property
    def bottom_side_draw(self):
        if self.bottom_split: return self.outs[-1]
    
    def _run(self):
        self.mixer._run()
        self.partition._run()
        for i in self.splitters: i._run()


class PhasePartition(Unit):
    _N_ins = 1
    _N_outs = 2
    strict_infeasibility_check = False
    
    def _init(self, phases, partition_data):
        self.partition_data = partition_data
        self.phases = phases
        self.solvent = None
        self.IDs = None
        self.K = None
        self.B = None
        self.T = None
        self.Q = 0.
        self.B_specification = None
    
    def _run(self, stacklevel=1, P=None, solvent=None, update=True,
             couple_energy_balance=True, decoupled_flash=None):
        if solvent is None: solvent = self.solvent
        else: self.solvent = solvent
        for i, j in zip(self.outs, self.phases): i.phase = j
        if update:
            ms = tmo.MultiStream.from_streams(self.outs)
            ms.copy_like(self.feed)
        else:
            ms = self.feed.copy()
            ms.phases = self.phases
        if ms.isempty(): return
        top, bottom = ms
        partition_data = self.partition_data
        if partition_data:
            self.K = K = partition_data['K']
            self.IDs = IDs = partition_data['IDs']
            args = (IDs, K, self.B / (1 + self.B) or partition_data['phi'], 
                    partition_data.get('extract_chemicals'),
                    partition_data.get('raffinate_chemicals'),
                    self.strict_infeasibility_check, stacklevel+1)
            phi = sep.partition(ms, top, bottom, *args)
            self.B = inf if phi == 1 else phi / (1 - phi)
            self.T = ms.T
        elif decoupled_flash:
            phi = sep.partition(
                ms, top, bottom, self.IDs, self.K, 0.5, 
                None, None, self.strict_infeasibility_check,
                stacklevel+1
            )
            return phi
        else:
            if 'g' in ms.phases:
                if couple_energy_balance:
                    B = self.B_specification
                    Q = self.Q
                    if B is None: 
                        H = ms.H + Q
                        V = None
                    else:
                        H = None
                        # B = V / (1 - V)
                        # B(1 - V) = V
                        # B - BV - V = 0
                        # -V(1 + B) + B = 0
                        V = B / (1 + B)
                        phi = V
                    ms.vle(P=P or ms.P, H=H, V=V)
                    index = ms.vle._index
                    IDs = ms.chemicals.IDs
                    IDs = tuple([IDs[i] for i in index])
                    L_mol = ms.imol['l', IDs]
                    L_total = L_mol.sum()
                    if L_total: 
                        x_mol = L_mol / L_total
                    else:
                        x_mol = 1
                    V_mol = ms.imol['g', IDs]
                    V_total = V_mol.sum()
                    if V_total: 
                        y_mol = V_mol / V_total
                    else:
                        y_mol = 0
                    K_new = y_mol / x_mol
                    if B is None: 
                        if V_total and not L_total:
                            self.B = inf
                        else:
                            self.B = V_total / L_total
                    self.T = ms.T
                else:
                    top, bottom = self.outs
                    if bottom.isempty():
                        if top.isempty(): return
                        p = top.dew_point_at_P(P)
                    else:
                        p = bottom.bubble_point_at_P(P)
                    # TODO: Note that solution decomposition method is bubble point
                    x = p.x
                    x[x == 0] = 1.
                    K_new = p.y / p.x
                    IDs = p.IDs
                    self.T = p.T
                    
            else:
                eq = ms.lle
                if update:
                    eq(T=ms.T, P=P, top_chemical=solvent, update=update)
                    lle_chemicals, K_new, phi = eq._lle_chemicals, eq._K, eq._phi
                else:
                    lle_chemicals, K_new, phi = eq(T=ms.T, P=P, top_chemical=solvent, update=update)
                self.B = phi / (1 - phi)
                self.T = ms.T
                if couple_energy_balance:
                    T = self.T
                    for i in self.outs: i.T = T
                IDs = tuple([i.ID for i in lle_chemicals])
            IDs_last = self.IDs
            if IDs_last and IDs_last != IDs:
                Ks = self.K
                for ID, K in zip(IDs, K_new): Ks[IDs_last.index(ID)] = K
            else:
                self.K = K_new
                self.IDs = IDs
            # if self.B_specification is not None and 'g' in ms.phases:
            #     phi = sep.compute_phase_fraction(ms.z_mol, self.K, self.B / (1 + self.B))
            #     self.B = phi / (1 - phi)

    
class MultiStageEquilibrium(Unit):
    """
    Create a MultiStageEquilibrium object that models counter-current 
    equilibrium stages.
    
    Parameters
    ----------
    N_stages : int
        Number of stages.
    feed_stages : tuple[int]
        Respective stage where feeds enter. Defaults to (0, -1).
    partition_data : {'IDs': tuple[str], 'K': 1d array}, optional
        IDs of chemicals in equilibrium and partition coefficients (molar 
        composition ratio of the extract over the raffinate or vapor over liquid). If given,
        The mixer-settlers will be modeled with these constants. Otherwise,
        partition coefficients are computed based on temperature and composition.
    solvent : str
        Name of main chemical in the solvent.
        
    Examples
    --------
    Simulate 2-stage extraction of methanol from water using octanol:
    
    >>> import biosteam as bst
    >>> bst.settings.set_thermo(['Water', 'Methanol', 'Octanol'], cache=True)
    >>> feed = bst.Stream('feed', Water=500, Methanol=50)
    >>> solvent = bst.Stream('solvent', Octanol=500)
    >>> MSE = bst.MultiStageEquilibrium(N_stages=2, ins=[feed, solvent], phases=('L', 'l'))
    >>> MSE.simulate()
    >>> extract, raffinate = MSE.outs
    >>> extract.imol['Methanol'] / feed.imol['Methanol'] # Recovery
    0.83
    >>> extract.imol['Octanol'] / solvent.imol['Octanol'] # Solvent stays in extract
    0.99
    >>> raffinate.imol['Water'] / feed.imol['Water'] # Carrier remains in raffinate
    0.82
    
    Simulate 10-stage extraction with user defined partition coefficients:
    
    >>> import numpy as np
    >>> feed = bst.Stream('feed', Water=5000, Methanol=500)
    >>> solvent = bst.Stream('solvent', Octanol=5000)
    >>> MSE = bst.MultiStageEquilibrium(N_stages=10, ins=[feed, solvent], phases=('L', 'l'),
    ...     partition_data={
    ...         'K': np.array([1.451e-01, 1.380e+00, 2.958e+03]),
    ...         'IDs': ('Water', 'Methanol', 'Octanol'),
    ...         'phi': 0.5899728891780545, # Initial phase fraction guess. This is optional.
    ...     }
    ... )
    >>> extract, raffinate = MSE.outs
    >>> MSE.simulate()
    >>> extract.imol['Methanol'] / feed.imol['Methanol'] # Recovery
    0.99
    >>> extract.imol['Octanol'] / solvent.imol['Octanol'] # Solvent stays in extract
    0.99
    >>> raffinate.imol['Water'] / feed.imol['Water'] # Carrier remains in raffinate
    0.82
    
    Because octanol and water do not mix well, it may be a good idea to assume
    that these solvents do not mix at all:
        
    >>> MSE = bst.MultiStageEquilibrium(N_stages=20, ins=[feed, solvent], phases=('L', 'l'),
    ...     partition_data={
    ...         'K': np.array([1.38]),
    ...         'IDs': ('Methanol',),
    ...         'raffinate_chemicals': ('Water',),
    ...         'extract_chemicals': ('Octanol',),
    ...     }
    ... )
    >>> MSE.simulate()
    >>> extract, raffinate = MSE.outs
    >>> extract.imol['Methanol'] / feed.imol['Methanol'] # Recovery
    0.99
    >>> extract.imol['Octanol'] / solvent.imol['Octanol'] # Solvent stays in extract
    1.0
    >>> raffinate.imol['Water'] / feed.imol['Water'] # Carrier remains in raffinate
    1.0
       
    Simulate with a feed at the 4th stage:
    
    >>> dilute_feed = bst.Stream('dilute_feed', Water=100, Methanol=2)
    >>> MSE = bst.MultiStageEquilibrium(N_stages=5, ins=[feed, dilute_feed, solvent], 
    ...     feed_stages=[0, 3, -1],
    ...     phases=('L', 'l'),
    ...     partition_data={
    ...         'K': np.array([1.38]),
    ...         'IDs': ('Methanol',),
    ...         'raffinate_chemicals': ('Water',),
    ...         'extract_chemicals': ('Octanol',),
    ...     }
    ... )
    >>> MSE.simulate()
    >>> extract, raffinate = MSE.outs
    >>> extract.imol['Methanol'] / (feed.imol['Methanol'] + dilute_feed.imol['Methanol']) # Recovery
    0.93
    
    Simulate with a 60% extract side draw at the 2nd stage:
    
    >>> MSE = bst.MultiStageEquilibrium(N_stages=5, ins=[feed, solvent],                         
    ...     top_side_draws={1: 0.6},
    ...     phases=('L', 'l'),
    ...     partition_data={
    ...         'K': np.array([1.38]),
    ...         'IDs': ('Methanol',),
    ...         'raffinate_chemicals': ('Water',),
    ...         'extract_chemicals': ('Octanol',),
    ...     }
    ... )
    >>> MSE.simulate()
    >>> extract, raffinate, extract_side_draw, *raffinate_side_draws = MSE.outs
    >>> (extract.imol['Methanol'] + extract_side_draw.imol['Methanol']) / feed.imol['Methanol'] # Recovery
    0.92
    
    Simulate stripping column with 2 stages
    
    >>> import biosteam as bst
    >>> bst.settings.set_thermo(['AceticAcid', 'EthylAcetate', 'Water', 'MTBE'], cache=True)
    >>> feed = bst.Stream('feed', Water=75, AceticAcid=5, MTBE=20, T=320)
    >>> steam = bst.Stream('steam', Water=100, phase='g', T=390)
    >>> MSE = bst.MultiStageEquilibrium(N_stages=2, ins=[feed, steam], feed_stages=[0, -1],
    ...     outs=['vapor', 'liquid'],
    ...     phases=('g', 'l'),
    ... )
    >>> MSE.simulate()
    >>> vapor, liquid = MSE.outs
    >>> vapor.imol['MTBE'] / feed.imol['MTBE']
    0.99
    >>> vapor.imol['Water'] / (feed.imol['Water'] + steam.imol['Water'])
    0.42
    >>> vapor.imol['AceticAcid'] / feed.imol['AceticAcid']
    0.74
    
    Simulate distillation column with 5 stages, a 0.673 reflux ratio, 
    2.57 boilup ratio, and feed at stage 2:
    
    >>> import biosteam as bst
    >>> bst.settings.set_thermo(['Water', 'Ethanol'], cache=True)
    >>> feed = bst.Stream('feed', Ethanol=80, Water=100, T=80.215 + 273.15)
    >>> MSE = bst.MultiStageEquilibrium(N_stages=5, ins=[feed], feed_stages=[2],
    ...     outs=['vapor', 'liquid'],
    ...     stage_specifications={0: ('Reflux', 0.673), -1: ('Boilup', 2.57)},
    ...     phases=('g', 'l'),
    ... )
    >>> MSE.simulate()
    >>> vapor, liquid = MSE.outs
    >>> vapor.imol['Ethanol'] / feed.imol['Ethanol']
    0.96
    >>> vapor.imol['Ethanol'] / vapor.F_mol
    0.69
    
    Simulate the same distillation column with a full condenser, 5 stages, a 0.673 reflux ratio, 
    2.57 boilup ratio, and feed at stage 2:
    
    >>> import biosteam as bst
    >>> bst.settings.set_thermo(['Water', 'Ethanol'], cache=True)
    >>> feed = bst.Stream('feed', Ethanol=80, Water=100, T=80.215 + 273.15)
    >>> MSE = bst.MultiStageEquilibrium(N_stages=5, ins=[feed], feed_stages=[2],
    ...     outs=['vapor', 'liquid', 'distillate'],
    ...     stage_specifications={0: ('Reflux', float('inf')), -1: ('Boilup', 2.57)},
    ...     bottom_side_draws={0: 0.673 / (1 + 0.673)}
    ... )
    >>> MSE.simulate()
    >>> vapor, liquid, distillate = MSE.outs
    >>> distillate.imol['Ethanol'] / feed.imol['Ethanol']
    0.81
    >>> distillate.imol['Ethanol'] / distillate.F_mol
    0.70
    
    """
    _N_ins = 2
    _N_outs = 2
    default_maxiter = 20
    default_fallback_maxiter = 3
    default_molar_tolerance = 0.1
    default_relative_molar_tolerance = 0.001
    default_algorithm = 'root'
    available_algorithms = {'root', 'optimize'}
    default_methods = {
        'root': 'fixed-point',
        'optimize': 'SLSQP',
    }
    
    #: Method definitions for convergence
    root_options: dict[str, tuple[Callable, bool, dict]] = {
        'fixed-point': (flx.conditional_fixed_point, True, {}),
    }
    optimize_options: dict[str, tuple[Callable, dict]] = {
        'SLSQP': (minimize, {'tol': 1e-3, 'method': 'SLSQP'})
    }
    auxiliary_unit_names = (
        'stages',
    )
    _side_draw_names = ('top_side_draws', 'bottom_side_draws')
    
    def __init_subclass__(cls, *args, **kwargs):
        super().__init_subclass__(cls, *args, **kwargs)
        if '_side_draw_names' in cls.__dict__:
            top, bottom = cls._side_draw_names
            setattr(
                cls, top, 
                property(
                    lambda self: self.top_side_draws,
                    lambda self, value: setattr(self, 'top_side_draws', value)
                )
            )
            setattr(
                cls, bottom, 
                property(
                    lambda self: self.bottom_side_draws,
                    lambda self, value: setattr(self, 'bottom_side_draws', value)
                )
            )
    
    def __init__(self,  ID='', ins=None, outs=(), thermo=None, **kwargs):
        if 'feed_stages' in kwargs: self._N_ins = len(kwargs['feed_stages'])
        top_side_draws, bottom_side_draws = self._side_draw_names
        N_outs = 2
        if top_side_draws in kwargs: N_outs += len(kwargs[top_side_draws]) 
        if bottom_side_draws in kwargs: N_outs += len(kwargs[bottom_side_draws]) 
        self._N_outs = N_outs
        Unit.__init__(self, ID, ins, outs, thermo, **kwargs)
    
    def _init(self,
            N_stages, 
            top_side_draws=None,
            bottom_side_draws=None, 
            feed_stages=None, 
            phases=None, 
            P=101325, 
            stage_specifications=None, 
            partition_data=None, 
            solvent=None, 
            use_cache=None,
            collapsed_init=True,
            algorithm=None,
            method=None,
            maxiter=None,
            inside_out=None,
        ):
        # For VLE look for best published algorithm (don't try simple methods that fail often)
        if phases is None: phases = ('g', 'l')
        if feed_stages is None: feed_stages = (0, -1)
        if stage_specifications is None: stage_specifications = {}
        elif not isinstance(stage_specifications, dict): stage_specifications = dict(stage_specifications)
        if top_side_draws is None: top_side_draws = {}
        elif not isinstance(top_side_draws, dict): top_side_draws = dict(top_side_draws)
        if bottom_side_draws is None: bottom_side_draws = {}
        elif not isinstance(bottom_side_draws, dict): bottom_side_draws = dict(bottom_side_draws)
        if partition_data is None: partition_data = {}
        self.multi_stream = tmo.MultiStream(None, P=P, phases=phases, thermo=self.thermo)
        self.N_stages = N_stages
        self.P = P
        phases = self.multi_stream.phases # Corrected order
        self._has_vle = 'g' in phases
        self._has_lle = 'L' in phases
        top_mark = 2 + len(top_side_draws)
        tsd_iter = iter(self.outs[2:top_mark])
        bsd_iter = iter(self.outs[top_mark:])
        last_stage = None
        self._top_split = top_splits = np.zeros(N_stages)
        self._bottom_split = bottom_splits = np.zeros(N_stages)
        self.stages = stages = []
        for i in range(N_stages):
            if last_stage is None:
                feed = ()
            else:
                feed = last_stage-1
            outs = []
            if i == 0:
                outs.append(
                    self-0, # extract or vapor
                )
            else:
                outs.append(bst.Stream(None))
            if i == N_stages - 1: 
                outs.append(
                    self-1 # raffinate or liquid
                )
            else:
                outs.append(
                    None
                )
            if i in top_side_draws:
                outs.append(next(tsd_iter))
                top_split = top_side_draws[i]
                top_splits[i] = top_split 
            else: 
                top_split = 0
            if i in bottom_side_draws:
                outs.append(next(bsd_iter))
                bottom_split = bottom_side_draws[i]
                bottom_splits[i] = bottom_split
            else: 
                bottom_split = 0
            
            new_stage = self.auxiliary(
                'stages', StageEquilibrium, phases=phases,
                ins=feed,
                outs=outs,
                partition_data=partition_data,
                top_split=top_split,
                bottom_split=bottom_split,
            )
            if last_stage:
                last_stage.add_feed(new_stage-0)
            last_stage = new_stage
        for feed, stage in zip(self.ins, feed_stages):
            stages[stage].add_feed(self.auxlet(feed))
        self._asplit_left = 1 - top_splits
        self._bsplit_left = 1 - top_splits
        self._asplit_1 = top_splits - 1
        self._bsplit_1 = bottom_splits - 1
        self.partitions = [i.partition for i in stages]
        self.solvent_ID = solvent
        self.partition_data = partition_data
        self.feed_stages = feed_stages
        self.top_side_draws = top_side_draws
        self.bottom_side_draws = bottom_side_draws
        
        #: dict[int, tuple(str, float)] Specifications for VLE by stage
        self.stage_specifications = stage_specifications
        for i, (name, value) in stage_specifications.items():
            B, Q = _get_specification(name, value)
            stages[i].set_specification(B=B, Q=Q)
            
        #: [int] Maximum number of iterations.
        self.maxiter = self.default_maxiter if maxiter is None else maxiter
        
        #: [int] Maximum number of iterations for fallback algorithm.
        self.fallback_maxiter = self.default_fallback_maxiter

        #: [float] Molar tolerance (kmol/hr)
        self.molar_tolerance = self.default_molar_tolerance

        #: [float] Relative molar tolerance
        self.relative_molar_tolerance = self.default_relative_molar_tolerance
        
        self.use_cache = True if use_cache else False
        
        self.collapsed_init = collapsed_init
        
        self.algorithm = self.default_algorithm if algorithm is None else algorithm
        
        self.method = self.default_methods[self.algorithm] if method is None else method
    
        self.inside_out = inside_out
    
    def correct_overall_mass_balance(self):
        outmol = sum([i.mol for i in self.outs])
        inmol = sum([i.mol for i in self.ins])
        try:
            factor = inmol / outmol
        except:
            pass
        else:
            for i in self.outs: i.mol *= factor
    
    def material_errors(self):
        errors = []
        stages = self.stages
        IDs = self.multi_stream.chemicals.IDs
        for stage in stages:
            errors.append(
                sum([i.imol[IDs] for i in stage.ins],
                    -sum([i.imol[IDs] for i in stage.outs]))
            )
        return pd.DataFrame(errors, columns=IDs)
    
    def set_flow_rates(self, top_flow_rates):
        top, bottom = self.multi_stream.phases
        stages = self.stages
        N_stages = self.N_stages
        range_stages = range(N_stages)
        index = self._update_index
        top_flow_rates[top_flow_rates < 0.] = 0.
        for i in range_stages:
            stage = stages[i]
            partition = stage.partition
            s_top, _ = partition.outs
            s_top.mol[index] = top_flow_rates[i]
            if stage.top_split: stage.splitters[0]._run()
        for i in range_stages:
            stage = stages[i]
            partition = stage.partition
            s_top, s_bottom = partition.outs
            bottom_flow = sum([i.mol for i in stage.ins], -s_top.mol)
            bottom_flow[bottom_flow < 0.] = 0.
            s_bottom.mol[:] = bottom_flow
            if stage.bottom_split: stage.splitters[-1]._run()
        self.correct_overall_mass_balance()
            
    def _run(self):
        if all([i.isempty() for i in self.ins]): 
            for i in self.outs: i.empty()
            return
        top_flow_rates = self.hot_start()
        algorithm = self.algorithm
        if algorithm == 'root':
            solver, conditional, options = self.root_options[self.method]
            try:
                if conditional:
                    solver(self._conditional_iter, top_flow_rates)
                else:
                    solver(self._root_iter, self.get_KTBs().flatten(), **options)
            except Converged: 
                pass
            else:    
                self.fallback_iter = 0
                if self.iter == self.maxiter:
                    flx.conditional_fixed_point(
                        self._sequential_iter, 
                        self.get_top_flow_rates()
                    )
        elif algorithm == 'optimize':
            solver, options = self.optimize_options[self.method]
            self.constraints = constraints = []
            stages = self.stages
            m, n = self.N_stages, self._N_chemicals
            last_stage = m - 1
            feed_flows, asplit_1, bsplit_1, _ = self._iter_args
            for i, stage in enumerate(stages):
                if i == 0:
                    args = (i,)
                    f = lambda x, i: feed_flows[i] - x[(i+1)*n:(i+2)*n] * asplit_1[i+1] - x[i*n:(i+1)*n] + 1e-6
                elif i == last_stage:
                    args_last = args
                    args = (i, f, args_last)
                    f = lambda x, i, f, args_last: feed_flows[i] + f(x, *args_last) - x[i*n:] + 1e-6
                else:
                    args_last = args
                    args = (i, f, args_last)
                    f = lambda x, i, f, args_last: feed_flows[i] + f(x, *args_last) - x[(i+1)*n:(i+2)*n] * asplit_1[i+1] - x[i*n:(i+1)*n] + 1e-6
                constraints.append(
                    dict(type='ineq', fun=f, args=args)
                )
            result = minimize(
                self._overall_error, 
                self.get_top_flow_rates_flat(),
                constraints=constraints,
                bounds=[(0, None)] * (m * n),
                **options,
            )
            print(result)
            self.set_flow_rates(result.x.reshape([m, n]))
        else:
            raise RuntimeError(
                f'invalid algorithm {algorithm!r}, only {self.available_algorithms} are allowed'
            )
    
    def _hot_start_phase_ratios_iter(self, 
            top_flow_rates, *args
        ):
        bottom_flow_rates = hot_start_bottom_flow_rates(
            top_flow_rates, *args
        )
        top_flow_rates = hot_start_top_flow_rates(
            bottom_flow_rates, *args
        )
        return top_flow_rates
        
    def hot_start_phase_ratios(self):
        stages = self.stages
        stage_index = []
        phase_ratios = []
        for i in list(self.stage_specifications):
            B = stages[i].partition.B_specification
            if B is None: continue 
            stage_index.append(i)
            phase_ratios.append(B)
        stage_index = np.array(stage_index, dtype=int)
        phase_ratios = np.array(phase_ratios, dtype=float)
        feeds = self.ins
        feed_stages = self.feed_stages
        top_feed_flows = 0 * self.feed_flows
        bottom_feed_flows = top_feed_flows.copy()
        top_flow_rates = top_feed_flows.copy()
        index = self._update_index
        for feed, stage in zip(feeds, feed_stages):
            if len(feed.phases) > 1 and 'g' in feed.phases:
                top_feed_flows[stage, :] += feed['g'].mol[index]
            elif feed.phase != 'g':
                continue
            else:
                top_feed_flows[stage, :] += feed.mol[index]
        for feed, stage in zip(feeds, feed_stages):
            if len(feed.phases) > 1 and 'g' not in feed.phases:
                bottom_feed_flows[stage, :] += feed['l'].mol[index]
            elif feed.phase == 'g': 
                continue
            else:
                bottom_feed_flows[stage, :] += feed.mol[index]
        feed_flows, asplit_1, bsplit_1, N_stages = self._iter_args
        args = (
            phase_ratios, np.array(stage_index), top_feed_flows,
            bottom_feed_flows, asplit_1, bsplit_1, N_stages
        )
        top_flow_rates = flx.wegstein(
            self._hot_start_phase_ratios_iter,
            top_flow_rates, args=args, xtol=self.relative_molar_tolerance
        )
        bottom_flow_rates = hot_start_bottom_flow_rates(
            top_flow_rates, *args
        )
        bf = bottom_flow_rates.sum(axis=1)
        bf[bf == 0] = 1e-32
        return top_flow_rates.sum(axis=1) / bf
    
    def hot_start_collapsed_stages(self,
            all_stages, feed_stages, stage_specifications,
            top_side_draws, bottom_side_draws,
        ):
        N_stages = len(all_stages)
        stage_map = {j: i for i, j in enumerate(sorted(all_stages))}
        feed_stages = [stage_map[i] for i in feed_stages]
        stage_specifications = {stage_map[i]: j for i, j in stage_specifications.items()}
        top_side_draws = {stage_map[i]: j for i, j in top_side_draws.items()}
        bottom_side_draws = {stage_map[i]: j for i, j in bottom_side_draws.items()}
        collapsed = self.auxiliary(
            'collapsed', 
            MultiStageEquilibrium,
            ins=self.ins,
            outs=self.outs,
            N_stages=N_stages,
            feed_stages=feed_stages,
            stage_specifications=stage_specifications,
            phases=self.multi_stream.phases,
            top_side_draws=top_side_draws,
            bottom_side_draws=bottom_side_draws,  
            P=self.P, 
            solvent=self.solvent_ID, 
            use_cache=self.use_cache,
            thermo=self.thermo
        )
        collapsed._run()
        collapsed_stages = collapsed.stages
        partitions = self.partitions
        stages = self.stages
        for i in range(self.N_stages):
            if i in all_stages:
                collapsed_partition = collapsed_stages[stage_map[i]].partition
                partition = partitions[i]
                partition.T = collapsed_partition.T
                partition.B = collapsed_partition.B
                T = collapsed_partition.T
                for i in partition.outs + stages[i].outs: i.T = T 
                partition.K = collapsed_partition.K
        self.interpolate_missing_variables()
                
    def hot_start(self):
        self.iter = 0
        ms = self.multi_stream
        feeds = self.ins
        feed_stages = self.feed_stages
        stages = self.stages
        partitions = self.partitions
        N_stages = self.N_stages
        top_phase, bottom_phase = ms.phases
        eq = 'vle' if top_phase == 'g' else 'lle'
        ms.mix_from(feeds)
        ms.P = self.P
        if eq == 'lle':
            self.solvent_ID = solvent_ID = self.solvent_ID or feeds[-1].main_chemical
        data = self.partition_data
        if data:
            top_chemicals = data.get('extract_chemicals') or data.get('vapor_chemicals')
            bottom_chemicals = data.get('raffinate_chemicals') or data.get('liquid_chemicals')
        if eq == 'lle':
            IDs = data['IDs'] if data else [i.ID for i in ms.lle_chemicals]
        else:
            IDs = data['IDs'] if data else [i.ID for i in ms.vle_chemicals]
        IDs = tuple(IDs)
        self._N_chemicals = N_chemicals = len(IDs)
        self._update_index = index = ms.chemicals.get_index(IDs)
        self.feed_flows = feed_flows = np.zeros([N_stages, N_chemicals])
        self.feed_enthalpies = feed_enthalpies = np.zeros(N_stages)
        for feed, stage in zip(feeds, feed_stages):
            feed_flows[stage, :] += feed.mol[index]
            feed_enthalpies[stage] = feed.H
        self._iter_args = (feed_flows, self._asplit_1, self._bsplit_1, self.N_stages)
        if self.collapsed_init and not data:
            feed_stages = [(i if i >= 0 else N_stages + i) for i in self.feed_stages]
            stage_specifications = {(i if i >= 0 else N_stages + i): j for i, j in self.stage_specifications.items()}
            top_side_draws = {(i if i >= 0 else N_stages + i): j for i, j in self.top_side_draws.items()}
            bottom_side_draws = {(i if i >= 0 else N_stages + i): j for i, j in self.bottom_side_draws.items()}
            all_stages = set([*feed_stages, *stage_specifications, *top_side_draws, *bottom_side_draws])
            collapsed_hot_start = self.collapsed_init and len(all_stages) != self.N_stages
        else:
            collapsed_hot_start = False
        if (self.use_cache 
            and all([i.IDs == IDs for i in partitions])): # Use last set of data
            pass
        elif collapsed_hot_start:
            self.hot_start_collapsed_stages(
                all_stages, feed_stages, stage_specifications,
                top_side_draws, bottom_side_draws,
            )
        else:
            if data: 
                top, bottom = ms
                K = data['K']
                phi = data.get('phi') or top.imol[IDs].sum() / ms.imol[IDs].sum()
                data['phi'] = phi = sep.partition(ms, top, bottom, IDs, K, phi,
                                                  top_chemicals, bottom_chemicals)
                B = inf if phi == 1 else phi / (1 - phi)
                T = ms.T
                for i in partitions: 
                    if i.B_specification is None: i.B = B
                    i.T = T
                    
            elif eq == 'lle':
                lle = ms.lle
                T = ms.T
                lle(T, top_chemical=solvent_ID)
                K = lle._K
                phi = lle._phi
                B = inf if phi == 1 else phi / (1 - phi)
                for i in partitions: 
                    if i.B_specification is None: i.B = B
                    i.T = T
                    for j in i.outs: j.T = T
            else:
                P = self.P
                if self.stage_specifications:
                    dp = ms.dew_point_at_P(P=P, IDs=IDs)
                    T_bot = dp.T
                    bp = ms.bubble_point_at_P(P=P, IDs=IDs)
                    T_top = bp.T
                    dT_stage = (T_bot - T_top) / N_stages
                    phase_ratios = self.hot_start_phase_ratios()
                    K = bp.y / bp.z
                    for i, B in enumerate(phase_ratios):
                        partition = partitions[i]
                        if partition.B_specification is None: partition.B = B
                        partition.T = T = T_top - i * dT_stage
                        for s in partition.outs: s.T = T
                else:
                    vle = ms.vle
                    vle(H=ms.H, P=P)
                    L_mol = ms.imol['l', IDs]
                    x_mol = L_mol / L_mol.sum()
                    V_mol = ms.imol['g', IDs]
                    y_mol = V_mol / V_mol.sum()
                    K = y_mol / x_mol
                    phi = ms.V
                    B = 1 / (1 - phi)
                    T = ms.T
                    for partition in partitions:
                        partition.T = T
                        partition.B = B
                        for i in partition.outs: i.T = T
            for i in partitions: i.K = K
            N_chemicals = len(index)
        if data:
            if top_chemicals:
                top_side_draws = self.top_side_draws
                F = np.zeros([N_stages, len(top_chemicals)])
                top_flow_rates = F.copy()
                for feed, stage in zip(feeds, feed_stages):
                    F[stage] = feed.imol[top_chemicals]
                A = np.eye(N_stages)
                for j, ID in enumerate(top_chemicals):
                    Aj = A.copy()
                    f = F[:, j]
                    for i in range(N_stages - 1):
                        Aj[i, i+1] = -1 
                    for i, value in top_side_draws.items():
                        Aj[i-1, i] *= (1 - value)    
                    top_flow_rates[:, j] = np.linalg.solve(Aj, f)
                for partition, a in zip(partitions, top_flow_rates):
                    partition.outs[0].imol[top_chemicals] = a
                for i in top_side_draws:
                    for s in stages[i].splitters: s._run()
            if bottom_chemicals:
                bottom_side_draws = self.bottom_side_draws
                F = np.zeros([N_stages, len(bottom_chemicals)])
                bottom_flow_rates = F.copy()
                for feed, stage in zip(feeds, feed_stages):
                    F[stage] = feed.imol[bottom_chemicals]
                A = np.eye(N_stages)
                for j, ID in enumerate(bottom_chemicals):
                    Aj = A.copy()
                    f = F[:, j]
                    for i in range(1, N_stages):
                        Aj[i, i-1] = -1 
                    for i, value in bottom_side_draws.items():
                        Aj[i+1, i] *= (1 - value)    
                    bottom_flow_rates[:, j] = np.linalg.solve(Aj, f)
                for partition, b in zip(partitions, bottom_flow_rates):
                    partition.outs[1].imol[bottom_chemicals] = b
                for i in bottom_side_draws:
                    for s in stages[i].splitters: s._run()
        for i in partitions: i.IDs = IDs
        return self.run_mass_balance()
    
    # TODO: This method is not working well. 
    # It should be mathematically the same as the departure method,
    # but it gives results that are extremely off.
    # def get_energy_balance_temperatures(self):
    #     # ENERGY BALANCE
    #     # Hv1 + Cv1*dT1 + Hl1 + Cl1*dT1 - Hv2 - Cv2*dT2 - Hl0 - Cl0*T0 = Q1
    #     # (Cv1 + Cl1)dT1 - Cv2*dT2 - Cl0*dT0 = Q1 - Hv1 - Hl1 + Hv2 + Hl0
    #     # C1*T1 - Cv2*T2 - Cl0*T0 = Q1 - H_out + H_in + C1*T1ref - Cv2*T2ref - Cl0*T0ref
    #     stages = self.stages
    #     N_stages = self.N_stages
    #     a = np.zeros(N_stages)
    #     b = a.copy()
    #     c = a.copy()
    #     d = a.copy()
    #     stage_mid = stages[0]
    #     stage_bottom = stages[1]
    #     partition_mid = stage_mid.partition
    #     partition_bottom = stage_bottom.partition
    #     C_out = sum([i.C for i in partition_mid.outs])
    #     C_bottom = stage_bottom.outs[0].C
    #     b[0] = C_out
    #     c[0] = -C_bottom
    #     Q = (partition_mid.Q or 0.) + C_out * partition_mid.T - C_bottom * partition_bottom.T
    #     if partition_mid.B_specification is not None:
    #         Q += stage_mid.H_in - partition_mid.H_out
    #     d[0] = Q
    #     for i in range(1, N_stages-1):
    #         stage_top = stage_mid
    #         stage_mid = stage_bottom
    #         stage_bottom = stages[i+1]
    #         partition_mid = partition_bottom
    #         partition_bottom = stage_bottom.partition
    #         C_out = sum([i.C for i in partition_mid.outs])
    #         C_top = stage_top.outs[1].C
    #         C_bottom = stage_bottom.outs[0].C
    #         a[i] = -C_top
    #         b[i] = C_out
    #         c[i] = -C_bottom
    #         Q = (partition_mid.Q or 0.) + C_out * partition_mid.T - C_bottom * partition_bottom.T - C_top * stage_top.partition.T
    #         if partition_mid.B_specification is not None:
    #             Q += stage_mid.H_in - partition_mid.H_out
    #         d[i] = Q
    #     stage_top = stage_mid
    #     stage_mid = stage_bottom
    #     partition_mid = partition_bottom
    #     C_out = sum([i.C for i in partition_mid.outs])
    #     C_top = stage_top.outs[1].C
    #     a[-1] = -C_top
    #     b[-1] = C_out
    #     Q = (partition_mid.Q or 0.) + C_out * partition_mid.T - C_top * stage_top.partition.T
    #     if partition_mid.B_specification is not None:
    #         Q += stage_mid.H_in - partition_mid.H_out
    #     d[-1] = Q
    #     return solve_TDMA(a, b, c, d)
    
    # Old slow algorithm, here for legacy purposes
    # def get_energy_balance_phase_ratio_departures(self):
    #     # ENERGY BALANCE
    #     # hv1*V1 + hl1*L1 - hv2*V2 - hl0*L0 = Q1
    #     # hV1*L1*B1 + hl1*L1 - hv2*L2*B2 - hl0*L0 = Q1
    #     # hV1*L1*B1 - hv2*L2*B2 = Q1 - Hl1 + Hl0
    #     # Hv1 + hV1*L1*dB1 - Hv2 - hv2*L2*dB2 = Q1 - Hl1 + Hl0
    #     # hV1*L1*dB1 - hv2*L2*dB2 = Q1 + H_in - H_out
    #     stages = self.stages
    #     N_stages = self.N_stages
    #     b = np.zeros(N_stages)
    #     c = b.copy()
    #     d = c.copy()
    #     for i in range(N_stages-1):
    #         stage_mid = stages[i]
    #         stage_bottom = stages[i+1]
    #         partition_mid = stage_mid.partition
    #         partition_bottom = stage_bottom.partition
    #         mid = partition_mid.B_specification is None
    #         bottom = partition_bottom.B_specification is None
    #         vapor, liquid = partition_mid.outs
    #         vapor_bottom, liquid_bottom = partition_bottom.outs
    #         if mid:
    #             if vapor.isempty():
    #                 liquid.phase = 'g'
    #                 b[i] = liquid.H
    #                 liquid.phase = 'l'
    #             else:
    #                 b[i] = vapor.h * liquid.F_mol
    #         if bottom:
    #             if vapor_bottom.isempty():
    #                 liquid_bottom.phase = 'g'
    #                 c[i] = liquid_bottom.H
    #                 liquid_bottom.phase = 'l'
    #             else:
    #                 c[i] = -vapor_bottom.h * liquid_bottom.F_mol
    #         Q = partition_mid.Q or 0.
    #         if mid: d[i] = Q + stage_mid.H_in - partition_mid.H_out
    #     if bottom:
    #         if vapor_bottom.isempty():
    #             liquid_bottom.phase = 'g'
    #             b[i] = liquid_bottom.H
    #             liquid_bottom.phase = 'l'
    #         else:
    #             b[-1] = vapor_bottom.h * liquid_bottom.F_mol
    #         Q = partition_bottom.Q or 0.
    #         d[-1] = Q + stage_bottom.H_in - partition_bottom.H_out
    #     return solve_RBDMA_1D_careful(b, c, d)
    
    # Old method, here for legacy purposes
    # def get_energy_balance_temperature_departures_old(self):
    #     # ENERGY BALANCE
    #     # Hv1 + Cv1*(dT1) + Hl1 + Cl1*dT1 - Hv2 - Cv2*dT2 - Hl0 - Cl0 = Q1
    #     # (Cv1 + Cl1)dT1 - Cv2*dT2 - Cl0*dT0 = Q1 - Hv1 - Hl1 + Hv2 + Hl0
    #     # C1dT1 - Cv2*dT2 - Cl0*dT0 = Q1 - H_out + H_in
    #     stages = self.stages
    #     N_stages = self.N_stages
    #     a = np.zeros(N_stages)
    #     b = a.copy()
    #     c = a.copy()
    #     d = a.copy()
    #     stage_mid = stages[0]
    #     stage_bottom = stages[1]
    #     partition_mid = stage_mid.partition
    #     partition_bottom = stage_bottom.partition
    #     C_out = sum([i.C for i in partition_mid.outs])
    #     C_bottom = stage_bottom.outs[0].C
    #     b[0] = C_out
    #     c[0] = -C_bottom
    #     Q = partition_mid.Q or 0.
    #     if partition_mid.B_specification is None: d[0] = Q + stage_mid.H_in - partition_mid.H_out
    #     for i in range(1, N_stages-1):
    #         stage_top = stage_mid
    #         stage_mid = stage_bottom
    #         stage_bottom = stages[i+1]
    #         partition_mid = partition_bottom
    #         partition_bottom = stage_bottom.partition
    #         C_out = sum([i.C for i in partition_mid.outs])
    #         C_top = stage_top.outs[1].C
    #         C_bottom = stage_bottom.outs[0].C
    #         a[i] = -C_top
    #         b[i] = C_out
    #         c[i] = -C_bottom
    #         Q = partition_mid.Q or 0.
    #         if partition_mid.B_specification is None: d[i] = Q + stage_mid.H_in - partition_mid.H_out
    #     stage_top = stage_mid
    #     stage_mid = stage_bottom
    #     partition_mid = partition_bottom
    #     C_out = sum([i.C for i in partition_mid.outs])
    #     C_top = stage_top.outs[1].C
    #     a[-1] = -C_top
    #     b[-1] = C_out
    #     Q = partition_mid.Q or 0.
    #     if partition_mid.B_specification is None: d[-1] = Q + stage_mid.H_in - partition_mid.H_out
    #     return solve_TDMA(a, b, c, d)
    
    def get_energy_balance_temperature_departures(self):
        partitions = self.partitions
        N_stages = self.N_stages
        Cl = np.zeros(N_stages)
        Cv = Cl.copy()
        Hv = Cl.copy()
        Hl = Cl.copy()
        specification_index = []
        for i, j in enumerate(partitions):
            top, bottom = j.outs
            Hl[i] = bottom.H
            Hv[i] = top.H
            Cl[i] = bottom.C
            Cv[i] = top.C
            if j.B_specification: specification_index.append(i)
        return temperature_departures(
            Cv, Cl, Hv, Hl, self._asplit_left, self._bsplit_left,
            N_stages, np.array(specification_index, int), self.feed_enthalpies
        )
    
    def get_energy_balance_phase_ratio_departures(self):
        # ENERGY BALANCE
        # hV1*L1*dB1 - hv2*L2*dB2 = Q1 + H_in - H_out
        partitions = self.partitions
        N_stages = self.N_stages
        L = np.zeros(N_stages)
        V = L.copy()
        hv = L.copy()
        hl = L.copy()
        specification_index = []
        for i, j in enumerate(partitions):
            top, bottom = j.outs
            Li = bottom.F_mol
            Vi = top.F_mol
            L[i] = Li
            V[i] = Vi
            if Vi == 0:
                bottom.phase = 'g'
                hv[i] = bottom.h
                bottom.phase = 'l'
            else:
                hv[i] = top.h
            if Li == 0:
                top.phase = 'l'
                hl[i] = bottom.h
                top.phase = 'g'
            else:
                hl[i] = bottom.h
            if j.B_specification: specification_index.append(i)
        return phase_ratio_departures(
            L, V, hl, hv, 
            self._asplit_1, 
            self._asplit_left,
            self._bsplit_left,
            N_stages,
            np.array(specification_index, dtype=int),
            self.feed_enthalpies,
        )
        
    def update_energy_balance_phase_ratios(self):
        dBs = self.get_energy_balance_phase_ratio_departures()
        for i, dB in zip(self.partitions, dBs):
            if i.B_specification is None: i.B += dB
    
    def update_energy_balance_temperatures(self):
        dTs = self.get_energy_balance_temperature_departures()
        dTs[np.abs(dTs) > 15] = 15
        for stage, dT in zip(self.stages, dTs):
            partition = stage.partition
            partition.T += dT
            for i in partition.outs: i.T += dT
       
    def run_mass_balance(self):
        partitions = self.partitions
        Sb, safe = bottoms_stripping_factors_safe(
            np.array([i.B for i in partitions]), 
            np.array([i.K for i in partitions]),
        )
        return top_flow_rates(Sb, *self._iter_args, safe)
       
    def update_mass_balance(self):
        self.set_flow_rates(self.run_mass_balance())
        
    def interpolate_missing_variables(self):
        stages = self.stages
        partitions = [i.partition for i in stages]
        phase_ratios = []
        partition_coefficients = []
        Ts = []
        N_stages = self.N_stages
        index = []
        lle = self._has_lle
        for i in range(N_stages):
            partition = partitions[i]
            B = partition.B
            T = partition.T
            K = partition.K
            if B is None or K is None or lle and (B <= 0 or B > 100): continue
            index.append(i)
            phase_ratios.append(B)
            partition_coefficients.append(K)
            Ts.append(T)
        N_ok = len(index)
        if len(index) == N_stages:
            phase_ratios = np.array(phase_ratios)
            partition_coefficients = np.array(partition_coefficients)
            Ts = np.array(Ts)
        else:
            if N_ok > 1:
                all_index = np.arange(N_stages)
                neighbors = get_neighbors(index, all_index)
                phase_ratios = fillmissing(neighbors, index, all_index, phase_ratios)
                Ts = fillmissing(neighbors, index, all_index, Ts)
                N_chemicals = self._N_chemicals
                all_partition_coefficients = np.zeros([N_stages, N_chemicals])
                for i in range(N_chemicals):
                    all_partition_coefficients[:, i] = fillmissing(
                        neighbors, index, all_index, 
                        [stage[i] for stage in partition_coefficients]
                    )
                partition_coefficients = all_partition_coefficients
            elif N_ok == 1:
                phase_ratios = np.array(N_stages * phase_ratios)
                partition_coefficients = np.array(N_stages * partition_coefficients)
                Ts = np.array(N_stages * Ts)
            elif N_ok == 0:
                raise RuntimeError('no phase equilibrium')
            for T, B, K, stage in zip(Ts, phase_ratios, partition_coefficients, stages): 
                partition = stage.partition
                partition.T = T 
                for i in partition.outs: i.T = T
                if partition.B_specification is None:
                    partition.B = B
                partition.K = K
    
    def set_KTBs(self, KTBs):
        lle = self._has_lle
        N_stages = self.N_stages 
        N_chemicals = self._N_chemicals
        N_flows = N_stages * N_chemicals
        K = KTBs[:N_flows]
        if lle: Ts = KTBs[N_flows:-N_stages]
        Bs = KTBs[-N_stages:]
        K = K.reshape([N_stages, N_chemicals])
        partitions = self.partitions
        N_chemicals = self._N_chemicals
        for i, partition in enumerate(partitions):
            if partition.B_specification is None: partition.B = Bs[i]
            if lle: partition.T = Ts[i]
            partition.K = KTBs[i]
    
    def get_KTBs(self):
        lle = self._has_lle
        N_stages = self.N_stages
        N_chemicals = self._N_chemicals
        N_flows = N_stages * N_chemicals
        KTBs = np.zeros(N_flows + (1 + lle) * N_stages)
        if lle: Ts = KTBs[N_flows:-N_stages]
        Bs = KTBs[-N_stages:]
        last_index = 0
        new_index = N_chemicals
        for i, partition in enumerate(self.partitions):
            KTBs[last_index: new_index] = partition.K
            if lle: Ts[i] = partition.T
            Bs[i] = partition.B
            last_index = new_index
            new_index += N_chemicals
        return KTBs
    
    def _overall_error(self, top_flow_rates):
        self._iter(
            top_flow_rates.reshape([self.N_stages, self._N_chemicals])
        ).flatten()
        H_out = np.array([i.H_out for i in self.stages])
        H_in = np.array([i.H_in for i in self.stages])
        diff = H_out - H_in
        diff_mask = np.abs(diff) > 1e-12
        diff = diff[diff_mask]
        denominator = H_out[diff_mask]
        H_in = H_in[diff_mask]
        denominator_mask = np.abs(denominator) < 1e-12
        denominator[denominator_mask] = H_in[denominator_mask]
        errors = diff / denominator
        MSE = (errors * errors).sum()
        return MSE
    
    def energy_balance_phase_ratio_iter(self, Bs):
        partitions = [i.partition for i in self.stages]
        for i, B in zip(partitions, Bs):
            if i.B_specification is None: i.B = B
        self.update_mass_balance()
        dBs = self.get_energy_balance_phase_ratio_departures()
        for i, partition in enumerate(partitions):
            if partition.B_specification is None:
                dBs[i] += partition.B
            else:
                dBs[i] = partition.B_specification
        return dBs
    
    def _iter(self, variables, KTBs=False):
        self.iter += 1
        if KTBs:
            self.set_KTBs(variables)
            self.update_mass_balance()
        else:
            self.set_flow_rates(variables)
            if self.method != 'fixed-point':
                for n, i in enumerate(self.partitions):
                    if i.B_specification is not None: continue
                    top, bottom = i.outs
                    bottom_mol = bottom.F_mol
                    i.B = top.F_mol / bottom_mol if bottom_mol else inf
        stages = self.stages
        P = self.P
        if self._has_vle: 
            for i in stages:
                mixer = i.mixer
                partition = i.partition
                mixer.outs[0].mix_from(
                    mixer.ins, energy_balance=False,
                )
                partition._run(P=P, update=False, 
                               couple_energy_balance=False)
                T = partition.T
                for i in (partition.outs + i.outs): i.T = T
            for i in range(2):
                self.update_mass_balance()
                self.update_energy_balance_phase_ratios()
        elif self._has_lle: # LLE
            for i in stages: 
                mixer = i.mixer
                partition = i.partition
                mixer._run()
                partition.T = mixer.outs[0].T
                partition._run(P=P, solvent=self.solvent_ID, update=False, 
                               couple_energy_balance=False)
            for i in stages:
                partition = i.partition
                T = partition.T
                for j in (i.outs + partition.outs): j.T = T
            self.update_energy_balance_temperatures()
        if self.inside_out and self._has_vle:
            self.update_mass_balance()
            N_stages = self.N_stages
            N_chemicals = self._N_chemicals
            T = np.zeros(N_stages)
            hv = T.copy()
            hl = T.copy()
            specification_index = []
            for i, j in enumerate(self.partitions):
                top, bottom = j.outs
                T[i] = j.T
                hl[i] = bottom.h
                hv[i] = top.h
                if j.B_specification is not None: specification_index.append(i)
            variables = solve_inside_loop(
                self.get_KTBs(), T, hv, hl, self.feed_flows,
                self._asplit_1, self._bsplit_1, 
                self._asplit_left, self._bsplit_left,
                N_stages, np.array(specification_index, int),
                N_chemicals,
                self.feed_enthalpies
            )
            if KTBs:
                return variables
            else:
                self.set_KTBs(variables)
                return self.run_mass_balance()
        elif KTBs:
            return self.get_KTBs()
        else:
            return self.run_mass_balance()

    def get_top_flow_rates_flat(self):
        N_chemicals = self._N_chemicals
        top_flow_rates = np.zeros(self.N_stages * N_chemicals)
        last_index = 0
        new_index = N_chemicals
        partition_index = self._update_index
        for i, partition in enumerate(self.partitions):
            top_flow_rates[last_index: new_index] = partition.outs[0].mol[partition_index]
            last_index = new_index
            new_index = last_index + N_chemicals
        return top_flow_rates
    
    def get_top_flow_rates(self):
        top_flow_rates = np.zeros([self.N_stages, self._N_chemicals])
        partition_index = self._update_index
        for i, partition in enumerate(self.partitions):
            top_flow_rates[i] = partition.outs[0].mol[partition_index]
        return top_flow_rates

    def _conditional_iter(self, top_flow_rates):
        mol = top_flow_rates.flatten()
        top_flow_rates_new = self._iter(top_flow_rates)
        mol_new = top_flow_rates_new.flatten()
        mol_errors = abs(mol - mol_new)
        if mol_errors.any():
            mol_error = mol_errors.max()
            if mol_error > 1e-12:
                nonzero_index, = (mol_errors > 1e-12).nonzero()
                mol_errors = mol_errors[nonzero_index]
                max_errors = np.maximum.reduce([abs(mol[nonzero_index]), abs(mol_new[nonzero_index])])
                rmol_error = (mol_errors / max_errors).max()
                not_converged = (
                    self.iter < self.maxiter and (mol_error > self.molar_tolerance
                     or rmol_error > self.relative_molar_tolerance)
                )
            else:
                not_converged = False
        else:
            not_converged = False
        return top_flow_rates_new, not_converged

    def _root_iter(self, KTBs):
        KTBs_new = self._iter(
            KTBs, True,
        )
        return KTBs_new - KTBs

    def _sequential_iter(self, top_flow_rates):
        self.fallback_iter += 1
        self.set_flow_rates(top_flow_rates)
        for i in self.stages: i._run()
        for i in reversed(self.stages): 
            try: i._run()
            except: breakpoint()
        mol = top_flow_rates.flatten()
        top_flow_rates = self.get_top_flow_rates()
        mol_new = top_flow_rates.flatten()
        mol_errors = abs(mol - mol_new)
        if mol_errors.any():
            mol_error = mol_errors.max()
            if mol_error > 1e-12:
                nonzero_index, = (mol_errors > 1e-12).nonzero()
                mol_errors = mol_errors[nonzero_index]
                max_errors = np.maximum.reduce([abs(mol[nonzero_index]), abs(mol_new[nonzero_index])])
                rmol_error = (mol_errors / max_errors).max()
                not_converged = (
                    self.fallback_iter < self.fallback_maxiter and (mol_error > self.molar_tolerance
                     or rmol_error > self.relative_molar_tolerance)
                )
            else:
                not_converged = False
        else:
            not_converged = False
        return top_flow_rates, not_converged


# %% General functional algorithms based on MESH equations to solve multi-stage 

@njit(cache=True)
def solve_TDMA(a, b, c, d): # Tridiagonal matrix solver
    """
    Solve a tridiagonal matrix using Thomas' algorithm.
    
    http://en.wikipedia.org/wiki/Tridiagonal_matrix_algorithm
    http://www.cfd-online.com/Wiki/Tridiagonal_matrix_algorithm_-_TDMA_(Thomas_algorithm)
    
    Notes
    -----
    `a` array starts from a1 (not a0).
    
    """
    n = d.shape[0] - 1 # number of equations minus 1
    for i in range(n):
        inext = i + 1
        m = a[i] / b[i]
        b[inext] = b[inext] - m * c[i] 
        d[inext] = d[inext] - m * d[i]
        
    b[n] = d[n] / b[n]

    for i in range(n-1, -1, -1):
        b[i] = (d[i] - c[i] * b[i+1]) / b[i]
    
    return b

@njit(cache=True)
def solve_TDMA_2D_careful(a, b, c, d, ab_fallback):
    n = d.shape[0] - 1 # number of equations minus 1
    for i in range(n):
        inext = i + 1
        ai = a[i]
        bi = b[i]
        m = bi.copy()
        inf_mask = bi == inf
        zero_mask = bi == 0
        ok_mask = ~inf_mask & ~zero_mask
        ok_index, = np.nonzero(ok_mask)
        inf_index, = np.nonzero(inf_mask)
        zero_index, = np.nonzero(zero_mask)
        special_index, = np.nonzero(inf_mask & (ai == -inf))
        special = ab_fallback[i]
        for j in inf_index: m[j] = 0
        for j in special_index: m[j] = special
        for j in zero_index: m[j] = inf
        for j in ok_index: m[j] = ai[j] / bi[j]
        b[inext] = b[inext] - m * c[i] 
        d[inext] = d[inext] - m * d[i]
        
    b[n] = d[n] / b[n]

    for i in range(n-1, -1, -1):
        b[i] = (d[i] - c[i] * b[i+1]) / b[i]
    return b

@njit(cache=True)
def solve_LBDMA(a, b, d): # Left bidiagonal matrix solver
    """
    Solve a left bidiagonal matrix using a reformulation of Thomas' algorithm.
    """
    n = d.shape[0] - 1 # number of equations minus 1
    for i in range(n):
        inext = i + 1
        m = a[i] / b[i]
        d[inext] = d[inext] - m * d[i]
    
    b[n] = d[n] / b[n]

    for i in range(n-1, -1, -1):
        b[i] = d[i] / b[i]
    return b

@njit(cache=True)
def solve_RBDMA_1D_careful(b, c, d):
    n = d.shape[0] - 1 # number of equations minus 1
    bn = b[n]
    dn = d[n]
    if bn == 0:
        if dn == 0:
            b[n] = 0
        else:
            b[n] = inf
    else:
        b[n] = d[n] / b[n]

    for i in range(n-1, -1, -1):
        bi = b[i]
        num = d[i] - c[i] * b[i+1]
        if bi == 0:
            if num == 0:
                b[i] = 0
            else:
                b[i] = inf
        else:
            b[i] = num / bi
    return b

@njit(cache=True)
def solve_RBDMA(b, c, d): # Right bidiagonal matrix solver
    """
    Solve a right bidiagonal matrix using a reformulation of Thomas' algorithm.
    """
    n = d.shape[0] - 1 # number of equations minus 1
    b[n] = d[n] / b[n]

    for i in range(n-1, -1, -1):
        b[i] = (d[i] - c[i] * b[i+1]) / b[i]
    return b

@njit(cache=True)
def hot_start_top_flow_rates(
        bottom_flows, phase_ratios, stage_index, top_feed_flows,
        bottom_feed_flows, asplit_1, bsplit_1, N_stages,
    ):
    """
    Solve a-phase flow rates for a single component across 
    equilibrium stages with side draws. 

    Parameters
    ----------
    bottom_flows : Iterable[1d array]
        Bottom flow rates by stages.
    phase_ratios : 1d array
        Phase ratios by stage. The phase ratio for a given stage is 
        defined as F_a / F_b; where F_a and F_b are the flow rates 
        of phase a (extract or vapor) and b (raffinate or liquid) leaving the stage 
        respectively.
    stage_index : 1d array
        Stage index for phase ratios.
    top_feed_flows : Iterable[1d array]
        Top flow rates of all components fed across stages. Shape should be 
        (N_stages, N_chemicals).
    bottom_feed_flows : Iterable [1d array]
        Bottom flow rates of all components fed across stages. Shape should be 
        (N_stages, N_chemicals).
    asplit_1 : 1d array
        Side draw split from phase a minus 1 by stage.
    bsplit_1 : 1d array
        Side draw split from phase b minus 1 by stage.

    Returns
    -------
    flow_rates_a: 2d array
        Flow rates of phase a with stages by row and components by column.

    """
    d = top_feed_flows.copy()
    b = d.copy()
    c = d.copy()
    for i in range(N_stages): 
        c[i] = asplit_1[i]
        b[i] = 1
    for n in range(stage_index.size):
        i = stage_index[n]
        B = phase_ratios[n]
        if B <= 1e-32:
            b[i] = inf
        else:
            b[i] += 1 / B 
        if i == 0:
            d[i] += bottom_feed_flows[i]
        else:
            d[i] += bottom_feed_flows[i] - bottom_flows[i - 1] * bsplit_1[i - 1]
    return solve_RBDMA(b, c, d)

@njit(cache=True)
def hot_start_bottom_flow_rates(
        top_flows, phase_ratios, stage_index, top_feed_flows,
        bottom_feed_flows, asplit_1, bsplit_1, N_stages
    ):
    """
    Solve a-phase flow rates for a single component across 
    equilibrium stages with side draws. 

    Parameters
    ----------
    bottom_flows : Iterable[1d array]
        Bottom flow rates by stages.
    phase_ratios : 1d array
        Phase ratios by stage. The phase ratio for a given stage is 
        defined as F_a / F_b; where F_a and F_b are the flow rates 
        of phase a (extract or vapor) and b (raffinate or liquid) leaving the stage 
        respectively.
    stage_index : 1d array
        Stage index for phase ratios.
    top_feed_flows : Iterable[1d array]
        Top flow rates of all components fed across stages. Shape should be 
        (N_stages, N_chemicals).
    bottom_feed_flows : Iterable [1d array]
        Bottom flow rates of all components fed across stages. Shape should be 
        (N_stages, N_chemicals).
    asplit_1 : 1d array
        Side draw split from phase a minus 1 by stage.
    bsplit_1 : 1d array
        Side draw split from phase b minus 1 by stage.

    Returns
    -------
    flow_rates_a: 2d array
        Flow rates of phase a with stages by row and components by column.

    """
    d = bottom_feed_flows.copy()
    b = d.copy()
    a = d.copy()
    for i in range(N_stages): 
        a[i] = bsplit_1[i]
        b[i] = 1
    last_stage = N_stages - 1
    for n in range(stage_index.size):
        i = stage_index[n]
        b[i] += phase_ratios[n]
        if i == last_stage:
            d[i] += top_feed_flows[i]
        else:
            d[i] += top_feed_flows[i] - top_flows[i + 1] * asplit_1[i + 1]
    return solve_LBDMA(a, b, d)

@njit(cache=True)
def bottoms_stripping_factors_safe(phase_ratios, partition_coefficients):
    """
    Return the bottoms stripping factors (i.e., the ratio of components in 
    the bottoms over the top) and a flag dictating whether it is safe for division
    and multiplication (i.e., whether 0 or inf are present).
    
    Parameters
    ----------
    phase_ratios : 1d array
        Phase ratios by stage. The phase ratio for a given stage is 
        defined as F_a / F_b; where F_a and F_b are the flow rates 
        of phase a (extract or vapor) and b (raffinate or liquid) leaving the stage 
        respectively.
    partition_coefficients : Iterable[1d array]
        Partition coefficients with stages by row and components by column.
        The partition coefficient for a component in a given stage is defined 
        as x_a / x_b; where x_a and x_b are the fraction of the component in 
        phase a (extract or vapor) and b (raffinate or liquid) leaving the stage.

    """
    zero_mask = phase_ratios <= 0.
    inf_mask = phase_ratios >= 1e32
    ok_mask = ~zero_mask & ~inf_mask
    phase_ratios = np.expand_dims(phase_ratios, -1)
    safe = ok_mask.all()
    if safe:
        # Bottoms stripping factor are, by definition, the ratio of components in the bottoms over the top.
        bottoms_stripping_factors = 1. / (phase_ratios * partition_coefficients)
    else:
        zero_index, = np.nonzero(zero_mask)
        inf_index, = np.nonzero(inf_mask)
        ok_index, = np.nonzero(ok_mask)
        bottoms_stripping_factors = np.zeros(partition_coefficients.shape)
        for i in ok_index:
            bottoms_stripping_factors[i] = 1. / (phase_ratios[i] * partition_coefficients[i])
        for i in zero_index:
            bottoms_stripping_factors[i] = inf
        for i in inf_index:
            bottoms_stripping_factors[i] = 0.
    return bottoms_stripping_factors, safe

@njit(cache=True)
def top_flow_rates(
        bottoms_stripping_factors, 
        feed_flows,
        asplit_1,
        bsplit_1,
        N_stages,
        safe,
    ):
    """
    Solve a-phase flow rates for a single component across equilibrium stages with side draws. 

    Parameters
    ----------
    bottoms_stripping_factors : Iterable[1d array]
        The ratio of component flow rates in phase b (raffinate or liquid) over
        the component flow rates in phase a (extract or vapor). 
    feed_flows : Iterable[1d array]
        Flow rates of all components fed across stages. Shape should be 
        (N_stages, N_chemicals).
    asplit_1 : 1d array
        Side draw split from phase a minus 1 by stage.
    bsplit_1 : 1d array
        Side draw split from phase b minus 1 by stage.

    Returns
    -------
    flow_rates_a : 2d array
        Flow rates of phase a with stages by row and components by column.

    """
    b = 1. + bottoms_stripping_factors
    c = asplit_1[1:]
    d = feed_flows.copy()
    a = np.expand_dims(bsplit_1, -1) * bottoms_stripping_factors
    if safe:    
        return solve_TDMA(a, b, c, d) 
    else:
        return solve_TDMA_2D_careful(a, b, c, d, bsplit_1)

# @njit(cache=True)
def phase_ratio_departures(
        L, V, hl, hv, asplit_1, asplit_left, bsplit_left, 
        N_stages, specification_index, H_feeds
    ):
    # hV1*L1*dB1 - hv2*L2*dB2 = Q1 + H_in - H_out
    b = hv * L
    c = b[1:] * asplit_1[1:]
    Hl_out = hl * L
    Hv_out = hv * V
    d = H_feeds - Hl_out - Hv_out
    Hl_in = (Hl_out * bsplit_left)[:-1]
    Hv_in = (Hv_out * asplit_left)[1:]
    d[1:] += Hl_in
    d[:-1] += Hv_in
    for i, j in enumerate(specification_index):
        b[j] = 0
        d[j] = 0
        jlast = j - 1
        if jlast > 0: c[jlast] = 0
    return solve_RBDMA_1D_careful(b, c, d)

@njit(cache=True)
def temperature_departures(Cv, Cl, Hv, Hl, asplit_left, bsplit_left,
                           N_stages, specification_index, H_feeds):
    # ENERGY BALANCE
    # C1dT1 - Cv2*dT2 - Cl0*dT0 = Q1 - H_out + H_in
    b = (Cv + Cl)
    a = -(Cl * bsplit_left)
    c = -(Cv * asplit_left)[1:]
    d = H_feeds - Hl - Hv
    d[1:] += (Hl * bsplit_left)[:-1]
    d[:-1] += (Hv * asplit_left)[1:]
    for i, j in enumerate(specification_index):
        a[j] = 0
        b[j] = 0
        d[j] = 0
        jlast = j - 1
        if jlast > 0: c[jlast] = 0
    return solve_TDMA(a, b, c, d)

def get_neighbors(index, all_index):
    size = all_index.size
    index_set = set(index)
    missing = set(all_index).difference(index)
    neighbors = []
    for i in missing:
        lb = i
        while lb > -1:
            lb -= 1
            if lb in index_set: break
        ub = i
        while ub < size:
            ub += 1
            if ub in index_set: break
        if ub == size:
            neighbors.append(
                (i, (lb,))
            )
        elif lb == -1:
            neighbors.append(
                (i, (ub,))
            )
        else:
            neighbors.append(
                (i, (lb, ub))
            )
    return neighbors

def fillmissing(all_neighbors, index, all_index, values):
    new_values = np.zeros_like(all_index, dtype=float)
    new_values[index] = values
    for i, neighbors in all_neighbors:
        if len(neighbors) == 2:
            lb, ub = neighbors
            lb_distance = i - lb
            ub_distance = ub - i
            sum_distance = lb_distance + ub_distance
            wlb = ub_distance / sum_distance
            wub = lb_distance / sum_distance
            x = wlb * new_values[lb] + wub * new_values[ub]
            new_values[i] = x
        else:
            new_values[i] = new_values[neighbors[0]]
    return new_values

# %% Methods for root finding

options = dict(ftol=1e-3, maxiter=100)
for name in ('anderson', 'diagbroyden', 'excitingmixing', 'linearmixing', 'broyden1', 'broyden2', 'krylov', 'hybr'):
    MultiStageEquilibrium.root_options[name] = (root, False, {'method': name, 'options': options})

# %% Russel's inside-out algorithm

def omega_approx(y, K):
    y_over_K = (y / K)
    return y_over_K / y_over_K.sum()

def Kb_init(y, K):
    omega = omega_approx(y, K)
    return np.exp((omega * np.log(K)).sum(axis=1))

def Kb_iter(alpha, x):
    return 1 / (alpha * x).sum(axis=1)

def alpha_approx(K, Kb):
    return K / Kb

def fit(x, y):
    xmean = x.mean()
    ymean = y.mean()
    xxmean = x - xmean
    m = (xxmean * (y - ymean)).sum() / (xxmean * xxmean).sum()
    b = ymean - m * xmean
    return m, b

def fit_partition_model(T, Kb):
    x = 1 / T
    y = np.log(Kb)
    xdiff = np.diff(x)
    ydiff = np.diff(y)
    M = y.copy()
    B = y.copy()
    M[:-1] = ydiff / xdiff
    B[:-1] = y - M * x
    M[-1] = M[-2]
    B[-1] = B[-2]
    return M, B

def h_approx(T, m, b):
    return m * T + b

def T_approx(Kb, m, b):
    return m / (np.log(Kb) - b)

def solve_inside_loop(KB, T, hv, hl, feed_flows,
                      asplit_1, bsplit_1, asplit_left, bsplit_left,
                      N_stages, specification_index, N_chemicals, H_feeds):
    N_flows = N_stages * N_chemicals
    K = KB[:N_flows]
    B = KB[N_flows:]
    K = K.reshape([N_stages, N_chemicals])
    Sb, safe = bottoms_stripping_factors_safe(B, K)
    top_flows = top_flow_rates(
        Sb, 
        feed_flows,
        asplit_1,
        bsplit_1,
        N_stages,
        safe,
    )
    top_flows[top_flows < 0] = 0
    dummy = top_flows.sum(axis=1, keepdims=True)
    dummy[dummy == 0] = 1
    y = top_flows / dummy
    Kb = Kb_init(y, K)
    Kb_coef = fit_partition_model(T, Kb)
    hv_coef = fit(T, hv)
    hl_coef = fit(T, hl)
    alpha = alpha_approx(K, Kb[:, np.newaxis])
    args = (alpha, Kb_coef, hv_coef, hl_coef, 
            feed_flows, asplit_1, bsplit_1,
            asplit_left, bsplit_left, N_stages, 
            specification_index, N_chemicals, H_feeds)
    KB_new = flx.fixed_point(inside_loop, KB.flatten(), xtol=1e-6, args=args)
    return KB_new.reshape([N_stages, N_chemicals + 1])
    
def inside_loop(KB, alpha, Kb_coef, hv_coef, hl_coef, 
                feed_flows, asplit_1, bsplit_1,
                asplit_left, bsplit_left, N_stages, 
                specification_index, N_chemicals, H_feeds):
    N_flows = N_stages * N_chemicals
    K = KB[:N_flows]
    B = KB[N_flows:]
    K = K.reshape([N_stages, N_chemicals])
    Sb, safe = bottoms_stripping_factors_safe(B, K)
    top_flows = top_flow_rates(
        Sb, 
        feed_flows,
        asplit_1,
        bsplit_1,
        N_stages,
        safe,
    )
    top_flows[top_flows < 0] = 0
    top_flows_net = top_flows.sum(axis=1)
    bottom_flows = Sb * top_flows
    bottom_flows_net = bottom_flows.sum(axis=1)
    dummy = bottom_flows_net.copy()
    dummy[dummy == 0] = 1e-12
    x = bottom_flows / dummy[:, np.newaxis]
    Kb = Kb_iter(alpha, x)
    KB_new = KB.copy()
    last_index = 0
    new_index = N_chemicals
    KB_new = KB.copy()
    for row in (alpha * Kb[:, np.newaxis]):
        KB_new[last_index: new_index] = last_index = new_index
        new_index = last_index + N_chemicals
    T = T_approx(Kb, *Kb_coef)
    hv = h_approx(T, *hv_coef)
    hl = h_approx(T, *hl_coef)
    print(T)
    print(hv)
    print(hl)
    breakpoint()
    KB_new[N_flows:] = B + phase_ratio_departures(
        bottom_flows_net, top_flows_net, hv, hl, asplit_1, 
        asplit_left, bsplit_left, N_stages,
        specification_index, H_feeds
    )
    return KB_new
    
    
    
    