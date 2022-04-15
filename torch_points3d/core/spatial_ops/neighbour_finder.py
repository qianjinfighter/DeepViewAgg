from abc import ABC, abstractmethod
from typing import List, Union, cast
import torch
import numpy as np
import faiss
from torch_geometric.nn import knn, radius
import torch_points_kernels as tp

from torch_points3d.utils.config import is_list
from torch_points3d.utils.enums import ConvolutionFormat

from torch_points3d.utils.debugging_vars import DEBUGGING_VARS, DistributionNeighbour


class BaseNeighbourFinder(ABC):
    def __call__(self, x, y, batch_x, batch_y):
        return self.find_neighbours(x, y, batch_x, batch_y)

    @abstractmethod
    def find_neighbours(self, x, y, batch_x, batch_y):
        pass

    def __repr__(self):
        return str(self.__class__.__name__) + " " + str(self.__dict__)


class RadiusNeighbourFinder(BaseNeighbourFinder):
    def __init__(self, radius: float, max_num_neighbors: int = 64, conv_type=ConvolutionFormat.MESSAGE_PASSING.value):
        self._radius = radius
        self._max_num_neighbors = max_num_neighbors
        self._conv_type = conv_type.lower()

    def find_neighbours(self, x, y, batch_x=None, batch_y=None):
        if self._conv_type == ConvolutionFormat.MESSAGE_PASSING.value:
            return radius(x, y, self._radius, batch_x, batch_y, max_num_neighbors=self._max_num_neighbors)
        elif self._conv_type == ConvolutionFormat.DENSE.value or ConvolutionFormat.PARTIAL_DENSE.value:
            return tp.ball_query(
                self._radius, self._max_num_neighbors, x, y, mode=self._conv_type, batch_x=batch_x, batch_y=batch_y
            )[0]
        else:
            raise NotImplementedError


class KNNNeighbourFinder(BaseNeighbourFinder):
    def __init__(self, k):
        self.k = k

    def find_neighbours(self, x, y, batch_x, batch_y):
        return knn(x, y, self.k, batch_x, batch_y)


class FAISSGPUKNNNeighbourFinder(BaseNeighbourFinder):
    def __init__(self, k, ncells=None, nprobes=10):
        """
        KNN on GPU with Facebook AI Similarity Search.

        Allows fast computation of KNN based on a voronoi-based division
        of search space and using GPU. Can be faster than sklearn under
        certain conditions:
            - k < 1024
            - nprobes < 1024
            - ncells tuned to training set size, typically with
            sqrt-like rule

        ncells controls the number of Voronoi cells created to divide
        the search space. These are built with k-means on the training
        set and act as the leaves of a kdtree. A heuristic was built to
        meet needs of two regimes, one for 'small' datasets of <10**7
        points and the other for 'larger' datasets of >10**7 points.
        ncells may not be optimal for any dataset, this does not affect
        accuracy much, but does affect speed.

        nprobes controls the number of cells visited during search. The
        larger, the slower but also the more accurate the neighbors.

        setting nprobes=1 is faster but causes erroneous neighborhoods
        at Voronoi cells boundaries.
        """
        self.k = k
        self.ncells = ncells
        self.nprobes = nprobes

    def find_neighbours(self, x, y, batch_x, batch_y):
        if batch_x is not None or batch_y is not None:
            raise NotImplementedError(
                "FAISSGPUKNNNeighbourFinder does not support batches yet")

        x = x.view(-1, 1) if x.dim() == 1 else x
        y = y.view(-1, 1) if y.dim() == 1 else y
        x, y = x.contiguous(), y.contiguous()

        # FAISS-GPU consumes numpy arrays
        x_np = x.cpu().numpy()
        y_np = y.cpu().numpy()

        # Initialization
        n_fit = x_np.shape[0]
        d = x_np.shape[1]
        nprobe = self.nprobes
        gpu = faiss.StandardGpuResources()

        # Heuristics to prevent k from being too large
        k_max = 1024
        k = min(self.k, n_fit, k_max)

        # Heuristic to parameterize the number of cells for FAISS index,
        # if not provided
        ncells = self.ncells
        if ncells is None:
            f1 = 3.5 * np.sqrt(n_fit)
            f2 = 1.6 * np.sqrt(n_fit)
            if n_fit > 2 * 10 ** 6:
                p = 1 / (1 + np.exp(2 * 10 ** 6 - n_fit))
            else:
                p = 0
            ncells = int(p * f1 + (1 - p) * f2)

        # Building a GPU IVFFlat index + Flat quantizer
        torch.cuda.empty_cache()
        quantizer = faiss.IndexFlatL2(d)  # the quantizer index
        index = faiss.IndexIVFFlat(quantizer, d, ncells, faiss.METRIC_L2)  # the main index
        gpu_index_flat = faiss.index_cpu_to_gpu(gpu, 0, index)  # pass index it to GPU
        gpu_index_flat.train(x_np)  # fit the cells to the training set distribution
        gpu_index_flat.add(x_np)

        # Querying the K-NN
        gpu_index_flat.setNumProbes(nprobe)
        return torch.LongTensor(gpu_index_flat.search(y_np, k)[1]).to(x.device)


class DilatedKNNNeighbourFinder(BaseNeighbourFinder):
    def __init__(self, k, dilation):
        self.k = k
        self.dilation = dilation
        self.initialFinder = KNNNeighbourFinder(k * dilation)

    def find_neighbours(self, x, y, batch_x, batch_y):
        # find the self.k * self.dilation closest neighbours in x for each y
        row, col = self.initialFinder.find_neighbours(x, y, batch_x, batch_y)

        # for each point in y, randomly select k of its neighbours
        index = torch.randint(self.k * self.dilation, (len(y), self.k), device=row.device, dtype=torch.long,)

        arange = torch.arange(len(y), dtype=torch.long, device=row.device)
        arange = arange * (self.k * self.dilation)
        index = (index + arange.view(-1, 1)).view(-1)
        row, col = row[index], col[index]

        return row, col


class BaseMSNeighbourFinder(ABC):
    def __call__(self, x, y, batch_x=None, batch_y=None, scale_idx=0):
        return self.find_neighbours(x, y, batch_x=batch_x, batch_y=batch_y, scale_idx=scale_idx)

    @abstractmethod
    def find_neighbours(self, x, y, batch_x=None, batch_y=None, scale_idx=0):
        pass

    @property
    @abstractmethod
    def num_scales(self):
        pass

    @property
    def dist_meters(self):
        return getattr(self, "_dist_meters", None)


class MultiscaleRadiusNeighbourFinder(BaseMSNeighbourFinder):
    """ Radius search with support for multiscale for sparse graphs

        Arguments:
            radius {Union[float, List[float]]}

        Keyword Arguments:
            max_num_neighbors {Union[int, List[int]]}  (default: {64})

        Raises:
            ValueError: [description]
    """

    def __init__(
        self, radius: Union[float, List[float]], max_num_neighbors: Union[int, List[int]] = 64,
    ):
        if DEBUGGING_VARS["FIND_NEIGHBOUR_DIST"]:
            if not isinstance(radius, list):
                radius = [radius]
            self._dist_meters = [DistributionNeighbour(r) for r in radius]
            if not isinstance(max_num_neighbors, list):
                max_num_neighbors = [max_num_neighbors]
            max_num_neighbors = [256 for _ in max_num_neighbors]

        if not is_list(max_num_neighbors) and is_list(radius):
            self._radius = cast(list, radius)
            max_num_neighbors = cast(int, max_num_neighbors)
            self._max_num_neighbors = [max_num_neighbors for i in range(len(self._radius))]
            return

        if not is_list(radius) and is_list(max_num_neighbors):
            self._max_num_neighbors = cast(list, max_num_neighbors)
            radius = cast(int, radius)
            self._radius = [radius for i in range(len(self._max_num_neighbors))]
            return

        if is_list(max_num_neighbors):
            max_num_neighbors = cast(list, max_num_neighbors)
            radius = cast(list, radius)
            if len(max_num_neighbors) != len(radius):
                raise ValueError("Both lists max_num_neighbors and radius should be of the same length")
            self._max_num_neighbors = max_num_neighbors
            self._radius = radius
            return

        self._max_num_neighbors = [cast(int, max_num_neighbors)]
        self._radius = [cast(int, radius)]

    def find_neighbours(self, x, y, batch_x=None, batch_y=None, scale_idx=0):
        if scale_idx >= self.num_scales:
            raise ValueError("Scale %i is out of bounds %i" % (scale_idx, self.num_scales))

        radius_idx = radius(
            x, y, self._radius[scale_idx], batch_x, batch_y, max_num_neighbors=self._max_num_neighbors[scale_idx]
        )
        return radius_idx

    @property
    def num_scales(self):
        return len(self._radius)

    def __call__(self, x, y, batch_x=None, batch_y=None, scale_idx=0):
        """ Sparse interface of the neighboorhood finder
        """
        return self.find_neighbours(x, y, batch_x, batch_y, scale_idx)


class DenseRadiusNeighbourFinder(MultiscaleRadiusNeighbourFinder):
    """ Multiscale radius search for dense graphs
    """

    def find_neighbours(self, x, y, scale_idx=0):
        if scale_idx >= self.num_scales:
            raise ValueError("Scale %i is out of bounds %i" % (scale_idx, self.num_scales))
        num_neighbours = self._max_num_neighbors[scale_idx]
        neighbours = tp.ball_query(self._radius[scale_idx], num_neighbours, x, y)[0]

        if DEBUGGING_VARS["FIND_NEIGHBOUR_DIST"]:
            for i in range(neighbours.shape[0]):
                start = neighbours[i, :, 0]
                valid_neighbours = (neighbours[i, :, 1:] != start.view((-1, 1)).repeat(1, num_neighbours - 1)).sum(
                    1
                ) + 1
                self._dist_meters[scale_idx].add_valid_neighbours(valid_neighbours)
        return neighbours

    def __call__(self, x, y, scale_idx=0, **kwargs):
        """ Dense interface of the neighboorhood finder
        """
        return self.find_neighbours(x, y, scale_idx)
