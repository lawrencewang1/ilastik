# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# Copyright 2011-2014, the ilastik developers


# basic python modules
import functools
import logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
from threading import Lock as ThreadLock

# required numerical modules
import numpy as np
import vigra
import opengm

# basic lazyflow types
from lazyflow.operator import Operator
from lazyflow.slot import InputSlot, OutputSlot
from lazyflow.rtype import SubRegion
from lazyflow.stype import Opaque
from lazyflow.request import Request, RequestPool

# required lazyflow operators
from lazyflow.operators.opLabelVolume import OpLabelVolume
from lazyflow.operators.valueProviders import OpArrayCache
from lazyflow.operators.opCompressedCache import OpCompressedCache
from lazyflow.operators.opReorderAxes import OpReorderAxes


## TODO @akreshuk documentation
#  - this operator assumes txyzc axis order
#  - only ROIs with 1 channel, 1 time slice are valid for slot Output
#  - requests to slot CachedOutput are guaranteed to be consistent
class OpGraphCut(Operator):
    name = "OpGraphCut"

    # prediction maps
    Prediction = InputSlot()

    # graph cut parameter
    Beta = InputSlot(value=.2)

    # segmentation image -> graph cut segmentation
    Output = OutputSlot()
    CachedOutput = OutputSlot()

    def __init__(self, *args, **kwargs):
        super(OpGraphCut, self).__init__(*args, **kwargs)

        cache = OpCompressedCache(parent=self)
        cache.name = "{}._cache".format(self.name)
        cache.Input.connect(self.Output)
        self._cache = cache

        self.CachedOutput.connect(self._cache.Output)

    def setupOutputs(self):
        # sanity checks
        shape = self.Prediction.meta.shape
        assert len(shape) == 5,\
            "Prediction maps must be a full 5d volume (txyzc)"
        tags = self.Prediction.meta.getAxisKeys()
        tags = "".join(tags)
        assert tags == 'txyzc',\
            "Prediction maps have wrong axes order"\
            "(expected: txyzc, got: {})".format(tags)

        self.Output.meta.assignFrom(self.Prediction.meta)
        # output is a binary image
        self.Output.meta.dtype = np.uint8

        # cache should hold entire c-t-slices in memory
        shape = list(self.Prediction.meta.shape)
        shape[0] = 1
        shape[4] = 1
        self._cache.BlockShape.setValue(tuple(shape))

    def execute(self, slot, subindex, roi, result):
        assert slot == self.Output, "Unknown slot requested: {}".format(slot)
        for i in (0, 4):
            assert roi.stop[i] - roi.start[i] == 1,\
                "Invalid roi for graph-cut: {}".format(str(roi))

        ## request the prediction image ##
        pred = self.Prediction.get(roi).wait()
        pred = vigra.taggedView(pred, axistags=self.Prediction.meta.axistags)
        pred = pred.withAxes(*'xyz')

        # prepare result
        resView = vigra.taggedView(result, axistags=self.Output.meta.axistags)
        resView = resView.withAxes(*'xyz')

        logger.info("Executing graph cut ... (this might take a while)")
        resView[:] = segmentGC_fast(pred, self.Beta.value)
        logger.info("Graph-cut done")

    def propagateDirty(self, slot, subindex, roi):
        # all input slots affect the (global) graph cut computation

        if slot == self.Beta:
            # beta value affects the whole volume
            self.Output.setDirty(slice(None))
        elif slot == self.Prediction:
            # time-channel slices are pairwise independent

            # determine t, c from input volume
            t_ind = 0
            c_ind = 4
            t = (roi.start[t_ind], roi.stop[t_ind])
            c = (roi.start[c_ind], roi.stop[c_ind])

            # set output dirty
            start = t[0:1] + (0,)*3 + c[0:1]
            stop = t[1:2] + self.Output.meta.shape[1:4] + c[1:2]
            roi = SubRegion(self.Output, start=start, stop=stop)
            self.Output.setDirty(roi)


##TODO @akreshuk documetation needed
def segmentGC_fast(pred, beta):
    nx, ny, nz = pred.shape

    numVar = pred.size
    numLabels = 2

    numberOfStates = np.ones(numVar, dtype=opengm.index_type)*numLabels
    gm = opengm.graphicalModel(numberOfStates, operator='adder')

    #Adding unary function and factors
    functions = np.zeros((numVar, 2))
    predflat = pred.reshape((numVar, 1))
    if (predflat.dtype == np.uint8):
        predflat = predflat.astype(np.float32)
        predflat = predflat/256.

    functions[:, 0] = 2*predflat[:, 0]
    functions[:, 1] = 1-2*predflat[:, 0]

    fids = gm.addFunctions(functions)
    gm.addFactors(fids, np.arange(0, numVar))

    #add one binary function (potts fuction)
    potts = opengm.PottsFunction([2, 2], 0.0, beta)
    fid = gm.addFunction(potts)

    #add binary factors
    nyz = ny*nz
    indices = np.arange(numVar,
                        dtype=np.uint32).reshape((nx, ny, nz))
    arg1 = np.concatenate([indices[:nx - 1, :, :], indices[1:, :, :]]
                            ).reshape((2, numVar - nyz)).transpose()

    arg2 = np.concatenate([indices[:, :ny - 1, :], indices[:, 1:, :]]
                            ).reshape((2, numVar - nx*nz)).transpose()

    arg3 = np.concatenate([indices[:, :, :nz - 1], indices[:, :, 1:]]
                            ).reshape((2, numVar - nx*ny)).transpose()

    gm.addFactors(fid, arg1)
    gm.addFactors(fid, arg2)
    gm.addFactors(fid, arg3)

    grcut = opengm.inference.GraphCut(gm)
    grcut.infer()
    argmin = grcut.arg()

    res = argmin.reshape((nx, ny, nz))
    return res
