import biosteam as bst
import thermosteam as tmo

F = 96485.33289  # Faraday's constant in C/mol

class ED_vfa(bst.Unit):
    _N_ins = 2
    _N_outs = 2

    def __init__(self, ID='', ins=None, outs=None, thermo=None,
                 CE_dict=None,  # Dictionary of charge efficiencies for each ion
                 j=8.23,   # Current density in A/m^2
                 t=3600*6,  # Time in seconds
                 A_m=0.0016,  # Membrane area in m^2
                 V=0.2/1000,  # Volume of all tanks in m^3
                 z_T=1.0,
                 r_m=3206.875,  # Areal Membrane resistance in Ohm*m^2
                 r_s=4961.875,  # Areal Solution resistance in Ohm*m^2
                 **kwargs):
        bst.Unit.__init__(self, ID, ins, outs, thermo)
        self.CE_dict = CE_dict or {'S_pro': 0.4, 'S_bu': 0.37, 'S_he': 0.23}  # Default CE for ions
        self.j = j
        self.t = t
        self.A_m = A_m
        self.V = V
        self.z_T = z_T
        self.r_m = r_m
        self.r_s = r_s

    @property
    def j(self):
        return self._j
    @j.setter
    def j(self, value):
        if value <= 0:
            raise ValueError("Current density must be positive.")
        self._j = value

    @property
    def t(self):
        return self._t
    @t.setter
    def t(self, value):
        if value <= 0:
            raise ValueError("Time must be positive.")
        self._t = value
        
    @property
    def A_m(self):
        return self._A_m
    @A_m.setter
    def A_m(self, value):
        if value <= 0:
            raise ValueError("Membrane area must be positive.")
        self._A_m = value

    @property
    def V(self):
        return self._V
    @V.setter
    def V(self, value):
        if value <= 0:
            raise ValueError("Volume must be positive.")
        self._V = value
        
    def _run(self):
        inf_dc, inf_ac = self.ins
        eff_dc, eff_ac = self.outs
    
        # Calculate total current [A]
        I = self.j * self.A_m
        self.total_current = I
    
        # Obtain the flow rates from the influent streams
        Q_dc = inf_dc.F_vol  # Flow rate from influent dilute stream in m^3/hr
        Q_ac = inf_ac.F_vol  # Flow rate from influent accumulated stream in m^3/hr
        self.Q_dc = Q_dc / 3600  # Convert to m^3/s
        self.Q_ac = Q_ac / 3600  # Convert to m^3/s
    
        self.n_T_dict = {}
        self.J_T_dict = {}
        self.influent_dc_conc = {}
        self.influent_ac_conc = {}
    
        # Calculate system resistance [Ohm]
        R_sys = self.A_m * (self.r_m + self.r_s)
        self.R_sys = R_sys
    
        # Calculate system voltage [V]
        V_sys = R_sys * I
        self.V_sys = V_sys
    
        # Calculate power consumption [W]
        P_sys = V_sys * I
        self.P_sys = P_sys
    
    def _design(self):
        D = self.design_results
        D['Membrane area'] = self.A_m
        D['Total current'] = self.j * self.A_m
        D['Tank volume'] = self.V
        D['System resistance'] = self.R_sys
        D['System voltage'] = self.V_sys
        D['Power consumption'] = self.P_sys

    def _cost(self):
        self.baseline_purchase_costs['Membrane'] = 100 * self.design_results['Membrane area']  # Assume $100 per m^2 for the membrane
        self.power_utility.consumption = self.design_results['Power consumption'] / 1000  # Assuming kWh consumption based on power