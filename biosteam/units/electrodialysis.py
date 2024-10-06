import biosteam as bst
import thermosteam as tmo
from thermosteam import settings, Chemicals, Chemical
from biorefineries import succinic
# Define chemicals
succinic.load()
f = succinic.flowsheet
u, s = f.unit, f.stream

#%%
fermented_conc_stream = u.S405.outs[1]
#%%

# Define input streams
inf_dc = bst.Stream('inf_dc', Water=5, **{'propionic acid': 5000, 'butyric acid': 5000, 'hexanoic acid': 5000}, units='kg/hr')
inf_ac = bst.Stream('inf_ac', Water=5, **{'NaCl': 500}, units='kg/hr')

eff_dc = bst.Stream('eff_dc')  # effluent dilute
eff_ac = bst.Stream('eff_ac')  # effluent accumulate

# Define the ED_vfa class (same as before)
class ED_vfa(bst.Unit):
    def __init__(self, ID='', ins=None, outs=None, thermo=None,
                 CE_dict=None,  # Dictionary of charge efficiencies for each ion
                 j=8.23,   # Current density in A/m^2
                 t=3600*6,  # Time in seconds
                 A_m=0.0016,  # Membrane area in m^2
                 V=0.2/1000,  # Volume of all tanks in m^3
                 z_T=1.0,
                 r_m=3206.875,  # Areal Membrane resistance in Ohm*m^2
                 r_s=4961.875,  # Areal Solution resistance in Ohm*m^2,
                 **kwargs):
        super().__init__(ID, ins, outs, thermo)
        self.CE_dict = CE_dict or {'propionic acid': 0.4, 'butyric acid': 0.37, 'hexanoic acid': 0.23}  # Default CE for ions
        self.j = j
        self.t = t
        self.A_m = A_m
        self.V = V
        self.z_T = z_T
        self.r_m = r_m
        self.r_s = r_s
        
    _N_ins = 2  # Number of input streams
    _N_outs = 2  # Number of output streams

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
        print(f"Total current (I): {I} A")
    
        # Obtain the flow rates from the influent streams
        Q_dc = inf_dc.F_vol  # Flow rate from influent dilute stream in m^3/hr
        Q_ac = inf_ac.F_vol  # Flow rate from influent accumulated stream in m^3/hr
        self.Q_dc = Q_dc / 3600  # Convert to m^3/s
        self.Q_ac = Q_ac / 3600  # Convert to m^3/s
    
        # Print original flow rates [m3/hr]
        print(f"Flow rate (Q_dc): {Q_dc} m^3/hr")
        print(f"Flow rate (Q_ac): {Q_ac} m^3/hr")
    
        # Print the converted flow rates [m3/s]
        print(f"Converted flow rate (Q_dc): {self.Q_dc} m^3/s")
        print(f"Converted flow rate (Q_ac): {self.Q_ac} m^3/s")
    
        # Calculate system resistance [Ohm]
        R_sys = self.A_m * (self.r_m + self.r_s)
        self.R_sys = R_sys
    
        # Calculate system voltage [V]
        V_sys = R_sys * I
        self.V_sys = V_sys
    
        # Calculate power consumption [W]
        P_sys = V_sys * I
        self.P_sys = P_sys
        print(f"System resistance (R_sys): {R_sys} Ohm")
        print(f"System voltage (V_sys): {V_sys} V")
        print(f"Power consumption (P_sys): {P_sys} W")
    
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

# Create ED_vfa unit
ED1 = ED_vfa(
    ID='ED1',
    ins=[inf_dc, inf_ac],
    outs=[eff_dc, eff_ac],
    j=5,
    t=288000,
    A_m=0.5,
    V=0.1
)

# Run the simulation
ED1.simulate()

# Display results
ED1.show()

# Print effluent concentrations
print("Effluent dilute stream concentrations (mol/L):")
for ion in ED1.CE_dict.keys():
    eff_dc_conc = eff_dc.imol[ion] / (eff_dc.F_vol * 1000)  # Convert to mol/L
    print(f"{ion}: {eff_dc_conc} mol/L")

print("Effluent accumulated stream concentrations (mol/L):")
for ion in ED1.CE_dict.keys():
    eff_ac_conc = eff_ac.imol[ion] / (eff_ac.F_vol * 1000)  # Convert to mol/L
    print(f"{ion}: {eff_ac_conc} mol/L")
