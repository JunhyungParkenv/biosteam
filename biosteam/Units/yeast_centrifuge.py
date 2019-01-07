# -*- coding: utf-8 -*-
"""
Created on Thu Aug 23 22:18:36 2018

@author: yoelr
"""

from .splitter import Splitter
from biosteam import Stream, Unit
from biosteam.exceptions import DesignError


class YeastCentrifuge(Unit):
    """Create a yeast centrifuge that separates out solids according to user defined split. Assume a continuous scroll solid bowl. 
    
    **Parameters**
    
        **split:** *[array_like]* Component splits to 0th output stream
        
        **solids:** *list[str]* IDs of solids 
    
    **ins**
    
        [:] Input streams
    
    **outs**
    
        [0] Liquid stream
        
        [1] Solids stream
    
    """
    
    kwargs = {'split': None,
              'solids': None}
    
    run = Splitter.run
    
    def cost(self):
        """
        * 'Centrifuge': (USD)
        """
        Cost = self.results['Cost']
        solids = self.kwargs['solids']
        index = Stream.get_index(solids)
        mass_solids = 0
        for s in self.ins:
            mass_solids += s.mass[index]
        ts = mass_solids.sum() # Total solids
        ts *= 0.0011023 # To short tons (2000 lbs/hr)
        if 2 < ts < 40:
            Cost['Centrifuge'] = self.CEPCI*68040/567*ts**0.50
        else:
            raise DesignError(f'Solids loading ({ts}) is not within 2 and 40 tonns')
        return Cost
