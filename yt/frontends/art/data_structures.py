"""
ART-specific data structures

Author: Matthew Turk <matthewturk@gmail.com>
Affiliation: UCSD
Homepage: http://yt.enzotools.org/
License:
  Copyright (C) 2010 Matthew Turk.  All Rights Reserved.

  This file is part of yt.

  yt is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as na
import stat
import weakref
import cPickle
import os
import struct

from yt.funcs import *
from yt.data_objects.grid_patch import \
      AMRGridPatch
from yt.data_objects.hierarchy import \
      AMRHierarchy
from yt.data_objects.static_output import \
      StaticOutput
from .fields import ARTFieldContainer
from yt.utilities.definitions import \
    mpc_conversion
from yt.utilities.io_handler import \
    io_registry
import yt.utilities.amr_utils as amr_utils

import yt.frontends.ramses._ramses_reader as _ramses_reader

def num_deep_inc(f):
    def wrap(self, *args, **kwargs):
        self.num_deep += 1
        rv = f(self, *args, **kwargs)
        self.num_deep -= 1
        return rv
    return wrap

class ARTGrid(AMRGridPatch):
    _id_offset = 0

    def __init__(self, id, hierarchy, level, locations, start_index):
        AMRGridPatch.__init__(self, id, filename = hierarchy.hierarchy_filename,
                              hierarchy = hierarchy)
        self.Level = level
        self.Parent = []
        self.Children = []
        self.locations = locations
        self.start_index = start_index.copy()

    def _setup_dx(self):
        # So first we figure out what the index is.  We don't assume
        # that dx=dy=dz , at least here.  We probably do elsewhere.
        id = self.id - self._id_offset
        if len(self.Parent) > 0:
            self.dds = self.Parent[0].dds / self.pf.refine_by
        else:
            LE, RE = self.hierarchy.grid_left_edge[id,:], \
                     self.hierarchy.grid_right_edge[id,:]
            self.dds = na.array((RE-LE)/self.ActiveDimensions)
        if self.pf.dimensionality < 2: self.dds[1] = 1.0
        if self.pf.dimensionality < 3: self.dds[2] = 1.0
        self.data['dx'], self.data['dy'], self.data['dz'] = self.dds

    def get_global_startindex(self):
        """
        Return the integer starting index for each dimension at the current
        level.
        """
        if self.start_index != None:
            return self.start_index
        if len(self.Parent) == 0:
            start_index = self.LeftEdge / self.dds
            return na.rint(start_index).astype('int64').ravel()
        pdx = self.Parent[0].dds
        start_index = (self.Parent[0].get_global_startindex()) + \
                       na.rint((self.LeftEdge - self.Parent[0].LeftEdge)/pdx)
        self.start_index = (start_index*self.pf.refine_by).astype('int64').ravel()
        return self.start_index

    def __repr__(self):
        return "ARTGrid_%04i (%s)" % (self.id, self.ActiveDimensions)

class ARTHierarchy(AMRHierarchy):

    grid = ARTGrid
    _handle = None
    
    def __init__(self, pf, data_style='art'):
        self.data_style = data_style
        self.field_info = ARTFieldContainer()
        self.parameter_file = weakref.proxy(pf)
        # for now, the hierarchy file is the parameter file!
        self.hierarchy_filename = self.parameter_file.parameter_filename
        self.directory = os.path.dirname(self.hierarchy_filename)

        self.float_type = na.float64
        AMRHierarchy.__init__(self,pf,data_style)

    def _initialize_data_storage(self):
        pass

    def _detect_fields(self):
        self.field_list = ['Density',]
    
    def _setup_classes(self):
        dd = self._get_data_reader_dict()
        AMRHierarchy._setup_classes(self, dd)
        self.object_types.sort()

    def _count_grids(self):
        # We have to do all the patch-coalescing here.
        #level_info is used by the IO so promoting it to the static
        # output class
        self.pf.level_info = [self.pf.ncell] # skip root grid for now
        #leve_info = []
        amr_utils.count_art_octs(
                self.pf.parameter_filename, self.pf.child_grid_offset,
                self.pf.min_level, self.pf.max_level, self.pf.nhydro_vars,
                self.pf.level_info)
        num_ogrids = sum(self.pf.level_info) + self.pf.iOctFree
        ogrid_left_indices = na.zeros((num_ogrids,3), dtype='int64') - 999
        ogrid_levels = na.zeros(num_ogrids, dtype='int64')
        ogrid_file_locations = na.zeros((num_ogrids,6), dtype='int64')
        ogrid_parents = na.zeros(num_ogrids, dtype="int64")
        ochild_masks = na.zeros((num_ogrids, 8), dtype='int64').ravel()
        amr_utils.read_art_tree(self.pf.parameter_filename, 
                                self.pf.child_grid_offset,
                                self.pf.min_level, self.pf.max_level,
                                ogrid_left_indices, ogrid_levels,
                                ogrid_parents, ochild_masks)
        ochild_masks.reshape((num_ogrids, 8), order="F")
        ogrid_levels[ogrid_left_indices[:,0] == -999] = 0
        # This bit of code comes from Chris, and I'm still not sure I have a
        # handle on what it does.
        final_indices =  ogrid_left_indices[na.where(ogrid_levels==self.pf.max_level)[0]]
        divisible=[na.all((final_indices%2**(level))==0) 
            for level in xrange(self.pf.max_level*2)]
        root_level = self.pf.max_level+na.where(na.logical_not(divisible))[0][0] 
        ogrid_dimension = na.zeros(final_indices.shape,dtype='int')+2
        ogrid_left_indices = ogrid_left_indices/2**(root_level - ogrid_levels[:,None]) - 1
        # Now we can rescale
        self.proto_grids = [[],]
        for level in xrange(1, len(self.pf.level_info)):
            if self.pf.level_info[level] == 0:
                self.proto_grids.append([])
                continue
            ggi = (ogrid_levels == level).ravel()
            mylog.info("Re-gridding level %s: %s octree grids", level, ggi.sum())
            nd = self.pf.domain_dimensions * 2**level
            dims = na.ones((ggi.sum(), 3), dtype='int64') * 2
            fl = ogrid_file_locations[ggi,:]
            # Now our initial protosubgrid
            #if level == 6: raise RuntimeError
            # We want grids that cover no more than MAX_EDGE cells in every direction
            MAX_EDGE = 128
            psgs = []
            left_index = ogrid_left_indices[ggi,:]
            right_index = left_index + 2
            lefts = [na.mgrid[0:nd[i]:MAX_EDGE] for i in range(3)]
            #lefts = zip(*[l.ravel() for l in lefts])
            pbar = get_pbar("Re-gridding ", lefts[0].size)
            min_ind = na.min(left_index, axis=0)
            max_ind = na.max(right_index, axis=0)
            for i,dli in enumerate(lefts[0]):
                pbar.update(i)
                if min_ind[0] > dli + nd[0]: continue
                if max_ind[0] < dli: continue
                idim = min(nd[0] - dli, MAX_EDGE)
                gdi = ((dli  <= right_index[:,0])
                     & (dli + idim >= left_index[:,0]))
                if not na.any(gdi): continue
                for dlj in lefts[1]:
                    if min_ind[1] > dlj + nd[1]: continue
                    if max_ind[1] < dlj: continue
                    idim = min(nd[1] - dlj, MAX_EDGE)
                    gdj = ((dlj  <= right_index[:,1])
                         & (dlj + idim >= left_index[:,1])
                         & (gdi))
                    if not na.any(gdj): continue
                    for dlk in lefts[2]:
                        if min_ind[2] > dlk + nd[2]: continue
                        if max_ind[2] < dlk: continue
                        idim = min(nd[2] - dlk, MAX_EDGE)
                        gdk = ((dlk  <= right_index[:,2])
                             & (dlk + idim >= left_index[:,2])
                             & (gdj))
                        if not na.any(gdk): continue
                        left = na.array([dli, dlj, dlk])
                        domain_left = left.ravel()
                        initial_left = na.zeros(3, dtype='int64') + domain_left
                        idims = na.ones(3, dtype='int64') * na.minimum(nd - domain_left, MAX_EDGE)
                        # We want to find how many grids are inside.
                        dleft_index = left_index[gdk,:]
                        dright_index = right_index[gdk,:]
                        ddims = dims[gdk,:]
                        dfl = fl[gdk,:]
                        psg = _ramses_reader.ProtoSubgrid(initial_left, idims,
                                        dleft_index, dright_index, ddims, dfl)
                        #print "Gridding from %s to %s + %s" % (
                        #    initial_left, initial_left, idims)
                        if psg.efficiency <= 0: continue
                        self.num_deep = 0
                        psgs.extend(self._recursive_patch_splitting(
                            psg, idims, initial_left, 
                            dleft_index, dright_index, ddims, dfl))
                        #psgs.extend([psg])
            pbar.finish()
            self.proto_grids.append(psgs)
            sums = na.zeros(3, dtype='int64')
            mylog.info("Final grid count: %s", len(self.proto_grids[level]))
            if len(self.proto_grids[level]) == 1: continue
            for g in self.proto_grids[level]:
                sums += [s.sum() for s in g.sigs]
            assert(na.all(sums == dims.prod(axis=1).sum()))
        self.num_grids = sum(len(l) for l in self.proto_grids)

    num_deep = 0

    @num_deep_inc
    def _recursive_patch_splitting(self, psg, dims, ind,
            left_index, right_index, gdims, fl):
        min_eff = 0.1 # This isn't always respected.
        if self.num_deep > 40:
            # If we've recursed more than 100 times, we give up.
            psg.efficiency = min_eff
            return [psg]
        if psg.efficiency > min_eff or psg.efficiency < 0.0:
            return [psg]
        tt, ax, fp = psg.find_split()
        if (fp % 2) != 0:
            if dims[ax] != fp + 1:
                fp += 1
            else:
                fp -= 1
        #print " " * self.num_deep + "Got ax", ax, "fp", fp
        dims_l = dims.copy()
        dims_l[ax] = fp
        li_l = ind.copy()
        if na.any(dims_l <= 0): return [psg]
        L = _ramses_reader.ProtoSubgrid(
                li_l, dims_l, left_index, right_index, gdims, fl)
        #print " " * self.num_deep + "L", tt, L.efficiency
        #if L.efficiency > 1.0: raise RuntimeError
        if L.efficiency <= 0.0: L = []
        elif L.efficiency < min_eff:
            L = self._recursive_patch_splitting(L, dims_l, li_l,
                    left_index, right_index, gdims, fl)
        else:
            L = [L]
        dims_r = dims.copy()
        dims_r[ax] -= fp
        li_r = ind.copy()
        li_r[ax] += fp
        if na.any(dims_r <= 0): return [psg]
        R = _ramses_reader.ProtoSubgrid(
                li_r, dims_r, left_index, right_index, gdims, fl)
        #print " " * self.num_deep + "R", tt, R.efficiency
        #if R.efficiency > 1.0: raise RuntimeError
        if R.efficiency <= 0.0: R = []
        elif R.efficiency < min_eff:
            R = self._recursive_patch_splitting(R, dims_r, li_r,
                    left_index, right_index, gdims, fl)
        else:
            R = [R]
        return L + R
        
    def _parse_hierarchy(self):
        # We have important work to do
        grids = []
        gi = 0
        for level, grid_list in enumerate(self.proto_grids):
            for g in grid_list:
                fl = g.grid_file_locations
                props = g.get_properties()
                self.grid_left_edge[gi,:] = props[0,:] / (2.0**(level+1))
                self.grid_right_edge[gi,:] = props[1,:] / (2.0**(level+1))
                self.grid_dimensions[gi,:] = props[2,:]
                self.grid_levels[gi,:] = level
                grids.append(self.grid(gi, self, level, fl, props[0,:]))
                gi += 1
        self.grids = na.array(grids, dtype='object')

    def _get_grid_parents(self, grid, LE, RE):
        mask = na.zeros(self.num_grids, dtype='bool')
        grids, grid_ind = self.get_box_grids(LE, RE)
        mask[grid_ind] = True
        mask = na.logical_and(mask, (self.grid_levels == (grid.Level-1)).flat)
        return self.grids[mask]

    def _populate_grid_objects(self):
        for gi,g in enumerate(self.grids):
            parents = self._get_grid_parents(g,
                            self.grid_left_edge[gi,:],
                            self.grid_right_edge[gi,:])
            if len(parents) > 0:
                g.Parent.extend(parents.tolist())
                for p in parents: p.Children.append(g)
            g._prepare_grid()
            g._setup_dx()
        self.max_level = self.grid_levels.max()

    def _setup_unknown_fields(self):
        for field in self.field_list:
            if field in self.parameter_file.field_info: continue
            mylog.info("Adding %s to list of fields", field)
            cf = None
            if self.parameter_file.has_key(field):
                def external_wrapper(f):
                    def _convert_function(data):
                        return data.convert(f)
                    return _convert_function
                cf = external_wrapper(field)
            add_field(field, lambda a, b: None,
                      convert_function=cf, take_log=False)

    def _setup_derived_fields(self):
        self.derived_field_list = []

    def _setup_data_io(self):
        pass
        #self.io = io_registry[self.data_style](self.tree_proxy)

class ARTStaticOutput(StaticOutput):
    _hierarchy_class = ARTHierarchy
    _fieldinfo_class = ARTFieldContainer
    _handle = None
    
    def __init__(self, filename, data_style='ramses',
                 storage_filename = None):
        StaticOutput.__init__(self, filename, data_style)
        self.storage_filename = storage_filename
        
        self.field_info = self._fieldinfo_class()
        self.current_time = 0.0
        self.dimensionality = 3
        self.refine_by = 2
        self.parameters["HydroMethod"] = 'ramses'
        self.parameters["Time"] = 1. # default unit is 1...
        
    def __repr__(self):
        return self.basename.rsplit(".", 1)[0]
        
    def _set_units(self):
        """
        Generates the conversion to various physical _units based on the parameter file
        """
        self.units = {}
        self.time_units = {}
        if len(self.parameters) == 0:
            self._parse_parameter_file()
        self._setup_nounits_units()
        self.conversion_factors = defaultdict(lambda: 1.0)
        self.time_units['1'] = 1
        self.units['1'] = 1.0
        self.units['unitary'] = 1.0 / (self.domain_right_edge - self.domain_left_edge).max()
        seconds = 1 #self["Time"]
        self.time_units['years'] = seconds / (365*3600*24.0)
        self.time_units['days']  = seconds / (3600*24.0)

    def _setup_nounits_units(self):
        z = 0
        mylog.warning("Setting 1.0 in code units to be 1.0 cm")
        if not self.has_key("TimeUnits"):
            mylog.warning("No time units.  Setting 1.0 = 1 second.")
            self.conversion_factors["Time"] = 1.0
        for unit in mpc_conversion.keys():
            self.units[unit] = mpc_conversion[unit] / mpc_conversion["cm"]

    def _parse_parameter_file(self):
        # We set our domain to run from 0 .. 1 since we are otherwise
        # unconstrained.
        self.domain_left_edge = na.zeros(3, dtype="float64")
        self.domain_right_edge = na.ones(3, dtype="float64")
        self.unique_identifier = \
            int(os.stat(self.parameter_filename)[stat.ST_CTIME])
        self.parameters = {}

        header_struct = [
            ('>i','pad byte'),
            ('>256s','jname'),
            ('>i','pad byte'),
            
            ('>i','pad byte'),
            ('>i','istep'),
            ('>d','t'),
            ('>d','dt'),
            ('>f','aexpn'),
            ('>f','ainit'),
            ('>i','pad byte'),
            
            ('>i','pad byte'),
            ('>f','boxh'),
            ('>f','Om0'),
            ('>f','Oml0'),
            ('>f','Omb0'),
            ('>f','hubble'),
            ('>i','pad byte'),
            
            ('>i','pad byte'),
            ('>i','nextras'),
            ('>i','pad byte'),

            ('>i','pad byte'),
            ('>f','extra1'),
            ('>f','extra2'),
            ('>i','pad byte'),

            ('>i','pad byte'),
            ('>256s','lextra'),
            ('>256s','lextra'),
            ('>i','pad byte'),
            
            ('>i', 'pad byte'),
            ('>i', 'min_level'),
            ('>i', 'max_level'),
            ('>i', 'pad byte'),
            ]
        
        f = open(self.parameter_filename, "rb")
        header_vals = {}
        for format, name in header_struct:
            size = struct.calcsize(format)
            # We parse single values at a time, so this will
            # always need to be indexed with 0
            output = struct.unpack(format, f.read(size))[0]
            header_vals[name] = output
        self.dimensionality = 3 # We only support three
        self.refine_by = 2 # Octree
        self.data_comment = header_vals['jname']
        self.current_time = header_vals['t']
        self.omega_lambda = header_vals['Oml0']
        self.omega_matter = header_vals['Om0'] - header_vals['Oml0']
        self.hubble_constant = header_vals['hubble']
        self.min_level = header_vals['min_level']
        self.max_level = header_vals['max_level']
        self.nhydro_vars = 10 #this gets updated later, but we'll default to this
        #nchem is nhydrovars-8, so we typically have 2 extra chem species 
        
        for to_skip in ['tl','dtl','tlold','dtlold','iSO']:
            _skip_record(f)

        (self.ncell,) = struct.unpack('>l', _read_record(f))
        # Try to figure out the root grid dimensions
        est = na.log2(self.ncell) / 3
        if int(est) != est: raise RuntimeError
        # Note here: this is the number of *cells* on the root grid.
        # This is not the same as the number of Octs.
        self.domain_dimensions = na.ones(3, dtype='int64') * int(2**est)

        self.root_grid_offset = f.tell()
        for to_skip in ['iOctCh', 'hvar', 'var']:
            _skip_record(f)

        self.iOctFree, self.nOct = struct.unpack('>ii', _read_record(f))
        self.child_grid_offset = f.tell()

        f.close()

    @classmethod
    def _is_valid(self, *args, **kwargs):
        if not os.path.basename(args[0]).startswith("info_"): return False
        fn = args[0].replace("info_", "amr_").replace(".txt", ".out00001")
        print fn
        return os.path.exists(fn)

def _skip_record(f):
    s = struct.unpack('>i', f.read(struct.calcsize('>i')))
    f.seek(s[0], 1)
    s = struct.unpack('>i', f.read(struct.calcsize('>i')))

def _read_record(f):
    s = struct.unpack('>i', f.read(struct.calcsize('>i')))[0]
    ss = f.read(s)
    s = struct.unpack('>i', f.read(struct.calcsize('>i')))
    return ss
