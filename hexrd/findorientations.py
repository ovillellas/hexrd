
from __future__ import print_function

import cPickle
import dill
import logging
import multiprocessing as mp
import os
import shelve
import sys
import time
from collections import namedtuple

import numpy as np
#np.seterr(over='ignore', invalid='ignore')

import scipy.cluster as cluster
import scipy.optimize as opt
from scipy import ndimage

import yaml

from hexrd import matrixutil as mutil
from hexrd.xrd import experiment as expt
from hexrd.xrd import indexer as idx
from hexrd.xrd import rotations as rot
from hexrd.xrd import transforms as xf
from hexrd.xrd import transforms_CAPI as xfcapi
from hexrd.coreutil import initialize_experiment

from hexrd.xrd import xrdutil

from hexrd.xrd import distortion as dFuncs

from hexrd.fitgrains import get_instrument_parameters

logger = logging.getLogger(__name__)

save_as_ascii = False           # FIX LATER...

# TODO: just require scikit-learn?
have_sklearn = False
try:
    import sklearn
    vstring = sklearn.__version__.split('.')
    if vstring[0] == '0' and int(vstring[1]) >= 14:
        import sklearn
        from sklearn.metrics.pairwise import pairwise_distances
        have_sklearn = True
except ImportError:
    pass

have_parallel_dbscan = False
try:
    import parallel_dbscan
    have_parallel_dbscan = True
except ImportError:
    pass

def generate_orientation_fibers(eta_ome, threshold, seed_hkl_ids, fiber_ndiv):
    """
    From ome-eta maps and hklid spec, generate list of
    quaternions from fibers
    """
    # seed_hkl_ids must be consistent with this...
    pd_hkl_ids = eta_ome.iHKLList[seed_hkl_ids]

    # grab angular grid infor from maps
    del_ome = eta_ome.omegas[1] - eta_ome.omegas[0]
    del_eta = eta_ome.etas[1] - eta_ome.etas[0]

    # labeling mask
    structureNDI_label = ndimage.generate_binary_structure(2, 2)

    # crystallography data from the pd object
    pd = eta_ome.planeData
    tTh  = pd.getTTh()
    bMat = pd.latVecOps['B']
    csym = pd.getLaueGroup()
    qsym = pd.getQSym()

    ############################################
    ##    Labeling of spots from seed hkls    ##
    ############################################

    qfib     = []
    labels   = []
    numSpots = []
    coms     = []
    for i in seed_hkl_ids:
        labels_t, numSpots_t = ndimage.label(
            eta_ome.dataStore[i] > threshold,
            structureNDI_label
            )
        coms_t = np.atleast_2d(
            ndimage.center_of_mass(
                eta_ome.dataStore[i],
                labels=labels_t,
                index=np.arange(1, np.amax(labels_t)+1)
                )
            )
        labels.append(labels_t)
        numSpots.append(numSpots_t)
        coms.append(coms_t)
        pass

    ############################################
    ##  Generate discrete fibers from labels  ##
    ############################################

    for i in range(len(pd_hkl_ids)):
        ii = 0
        qfib_tmp = np.empty((4, fiber_ndiv*numSpots[i]))
        for ispot in range(numSpots[i]):
            if not np.isnan(coms[i][ispot][0]):
                ome_c = eta_ome.omeEdges[0] \
                        + (0.5 + coms[i][ispot][0])*del_ome
                eta_c = eta_ome.etaEdges[0] \
                        + (0.5 + coms[i][ispot][1])*del_eta

                #gVec_s = xrdutil.makeMeasuredScatteringVectors(
                #    tTh[pd_hkl_ids[i]], eta_c, ome_c
                #    )
                gVec_s = xfcapi.anglesToGVec(
                    np.atleast_2d(
                        [tTh[pd_hkl_ids[i]], eta_c, ome_c]
                        )
                    ).T

                tmp = mutil.uniqueVectors(
                    rot.discreteFiber(
                        pd.hkls[:, pd_hkl_ids[i]].reshape(3, 1),
                        gVec_s,
                        B=bMat,
                        ndiv=fiber_ndiv,
                        invert=False,
                        csym=csym
                        )[0]
                    )
                jj = ii + tmp.shape[1]
                qfib_tmp[:, ii:jj] = tmp
                ii += tmp.shape[1]
                pass
            pass
        qfib.append(qfib_tmp[:, :ii])
        pass
    return np.hstack(qfib)


_clustering_option = namedtuple('_clustering_option', ['fn', 'fallback'])

_clustering_algorithm_dict = {}

class ClusterMethodUnavailableError(Exception):
    pass

def get_supported_clustering_algorithms():
    """get a list of the supported clustering algorithms"""
    # note: this is used by the yaml parser to know available
    #       options
    return _clustering_algorithm_dict.keys()


def clustering_algorithm(key, fallback=None):
    """A decorator that registers clustering algorithms automagically.

    A valid cluster algorithm must return an array of [int] indices
    that map each fiber to its corresponding cluster id.

    Valid clusters id are positive integers, with 0 reserved for noise
    fibers where applicable.

    """
    def wrapper(fn):
        assert key not in _clustering_algorithm_dict
        val = _clustering_option(fn, fallback)
        _clustering_algorithm_dict.update({key: val })
        return fn

    return wrapper


@clustering_algorithm('qim-dbscan')
def cluster_quaternion_im_dbscan(qfib_r, qsym, cl_radius, min_samples):
    if not have_sklearn:
        raise ClusterMethodUnavailableError('required module sklearn >= 0.14 not found.')

    quaternion_im = np.ascontiguousarray(qfib_r[1:,:].T)
    dbscan = sklearn.cluster.dbscan
    _, labels = dbscan(quaternion_im,
                       eps=np.sin(0.5*np.radians(cl_radius)),
                       min_samples=min_samples)
    return labels.astype(int, copy=False) + 1


@clustering_algorithm('omp-dbscan', fallback='homochoric-dbscan')
def cluster_parallel_dbscan(qfib_r, qsym, cl_radius, min_samples):
    if not have_parallel_dbscan:
        raise ClusterMethodUnavailableError('required module parallel_dbscan not found.')
    homochoric_coords = xfcapi.homochoricOfQuat(qfib_r)
    labels = parallel_dbscan.omp_dbscan(
        homochoric_coords,
        eps=np.radians(cl_radius),
        min_samples=min_samples)
    return labels.astype(int, copy=False) + 1


@clustering_algorithm('homochoric-dbscan', fallback='dbscan')
def cluster_homochoric_dbscan(qfib_r, qsym, cl_radius, min_samples):
    if not have_sklearn:
        raise ClusterMethodUnavailableError('required module sklearn >= 0.14 not found.')

    homochoric_coords = xfcapi.homochoricOfQuat(qfib_r)
    dbscan = sklearn.cluster.dbscan
    _, labels = dbscan(
        homochoric_coords,
        eps=np.radians(cl_radius),
        min_samples=min_samples)
    return labels.astype(int, copy=False) + 1


@clustering_algorithm('fclusterdata')
def cluster_fcluster(qfib_r, qsym,  cl_radius, min_samples):
    qsym = np.array(qsym.T, order='C').T
    def quat_distance(x, y):
        return xfcapi.quat_distance(np.array(x, order='C'), np.array(y, order='C'), qsym)

    cl = cluster.hierarchy.fclusterdata(
        qfib_r.T,
        np.radians(cl_radius),
        criterion='distance',
        metric=quat_distance
    )
    return cl.astype(int, copy=False)


@clustering_algorithm('qim-fclusterdata', 'fclusterdata')
def cluster_qim_fclusterdata(qfib_r, qym, cl_radius, min_samples):
    if not have_sklearn:
        raise ClusterMethodUnavailableError('required module sklearn >= 0.14 not found.')

    quaternion_im = np.ascontiguousarray(qfib_r[1:,:].T)
    dbscan = sklearn.cluster.dbscan
    labels = cluster.hierarchy.fclusterdata(
        quaternion_im,
        np.sin(0.5*np.radians(cl_radius)),
        criterion='distance')
    return labels.astype(int, copy=False) + 1


@clustering_algorithm('dbscan', 'fclusterdata')
def cluster_dbscan(qfib_r, qsym, cl_radius, min_samples):
    if not have_sklearn:
        raise ClusterMethodUnavailableError('required module sklearn >= 0.14 not found.')

    qsym = np.array(qsym.T, order='C').T
    def quat_distance(x, y):
        return xfcapi.quat_distance(np.array(x, order='C'), np.array(y, order='C'), qsym)

    pdist = pairwise_distances(
        qfib_r.T, metric=quat_distance, n_jobs=1
    )

    dbscan = sklearn.cluster.dbscan

    _, labels = dbscan(
        pdist,
        eps=np.radians(cl_radius),
        min_samples=min_samples,
        metric='precomputed'
    )
    return labels.astype(int, copy=False) + 1


def compute_centroids(qfib_r, cl, qsym):
    """compute a centroid quaternion for each cluster.

    qfib_r: array[4, nquat]
        source quaternion array

    cl: array[nquat]
        cluster indices

    qsym:

    returns: array[4:nclusters]
        nclusters is the number of unique cluster ids in cl,
        ignoring id 0 if present (cluster for noise fibers).
    """
    clusters = np.unique(cl)
    assert clusters[0] >= 0
    if clusters[0] == 0:
        # ignore the '0' cluster
        clusters = clusters[1:]

    qbar = np.zeros((4, len(clusters)))
    # this can be made more efficient probably... but...
    for i, cl_id in enumerate(clusters):
        quats_in_cluster = np.ascontiguousarray(qfib_r[:, cl==cl_id])
        npts = sum(cl == i + 1)

        qbar[:, i] = rot.quatAverage(quats_in_cluster, qsym).flatten()

    return qbar


def run_cluster(compl, qfib, qsym, cfg, min_samples=None, compl_thresh=None, radius=None):
    """
    """
    algorithm = cfg.find_orientations.clustering.algorithm

    cl_radius = cfg.find_orientations.clustering.radius
    min_compl = cfg.find_orientations.clustering.completeness

    # check for override on completeness threshold
    if compl_thresh is not None:
        min_compl = compl_thresh

    # check for override on radius
    if radius is not None:
        cl_radius = radius

    start = time.clock() # time this

    num_above = sum(np.array(compl) > min_compl)
    if num_above == 0:
        # nothing to cluster
        qbar = cl = np.array([])
    elif num_above == 1:
        # short circuit
        qbar = qfib[:, np.array(compl) > min_compl]
        cl = [1]
    else:
        # use compiled module for distance
        # just to be safe, must order qsym as C-contiguous

        qfib_r = np.ascontiguousarray(qfib[:, np.array(compl) > min_compl])

        logger.info(
            "Feeding %d orientations above %.1f%% to clustering",
            qfib_r.shape[1], 100*min_compl
            )

        cl_dict = _clustering_algorithm_dict
        cluster_args = [qfib_r, qsym, cl_radius, min_samples]
        while algorithm is not None:
            if algorithm not in cl_dict:
                raise RuntimeError(
                    "Clustering '{0}' not recognized".format(algorithm)
                    )
            try:
                logger.info("Trying '%s' over %d orientations",
                            algorithm, qfib_r.shape[1])
                cl = cl_dict[algorithm].fn(*cluster_args)
                algorithm_used = algorithm
                algorithm = None
            except ClusterMethodUnavailableError as error:
                logger.info(error.msg)
                fb = cl_dict[algorithm].fallback
                if fb is None:
                    msg = "Clustering '{0}' failed, no fallback."
                    raise RuntimeError(msg.format(algorithm))
                msg = "Clustering '{0}' failed, trying '{1}'."
                logger.info(msg.format(algorithm, fb))
                algorithm = fb

        nblobs = len(np.unique(cl))

        logger.info(
            "clustering done in %f seconds, computing centroids",
            time.clock() - start
        )

        np.savetxt(
            os.path.join(cfg.working_dir, 'clusters.dat'),
            cl,
            fmt='%5u'
        )

        # Compute the quaternion average for the different clusters
        qbar = compute_centroids(qfib_r, cl, qsym)

    logger.info("clustering took %f seconds", time.clock() - start)
    logger.info(
        "Found %d orientation clusters with >=%.1f%% completeness"
        " and %2f misorientation",
        qbar.size/4,
        100.*min_compl,
        cl_radius
        )

    return np.atleast_2d(qbar), cl


def load_eta_ome_maps(cfg, pd, reader, detector, hkls=None, clean=False):
    fn = os.path.join(
        cfg.working_dir,
        cfg.find_orientations.orientation_maps.file
        )
    
    if not clean:
        try:
            res = cPickle.load(open(fn, 'r'))
            pd = res.planeData
            available_hkls = pd.hkls.T
            logger.info('loaded eta/ome orientation maps from %s', fn)
            hkls = [str(i) for i in available_hkls[res.iHKLList]]
            logger.info(
                'hkls used to generate orientation maps: %s', hkls)
            return res
        except (AttributeError, IOError):
            return generate_eta_ome_maps(cfg, pd, reader, detector, hkls)
    else:
        logger.info('clean option specified; recomputing eta/ome orientation maps')
        return generate_eta_ome_maps(cfg, pd, reader, detector, hkls)

    
def generate_eta_ome_maps(cfg, pd, reader, detector, hkls=None):

    available_hkls = pd.hkls.T
    # default to all hkls defined for material
    active_hkls = range(available_hkls.shape[0])
    # override with hkls from config, if specified
    temp = cfg.find_orientations.orientation_maps.active_hkls
    active_hkls = active_hkls if temp == 'all' else temp
    # override with hkls from command line, if specified
    active_hkls = hkls if hkls is not None else active_hkls

    logger.info(
        "using hkls to generate orientation maps: %s",
        ', '.join([str(i) for i in available_hkls[active_hkls]])
        )

    # not ready # eta_ome = xrdutil.EtaOmeMaps(cfg, reader=reader, eta_step=None)
    bin_frames = cfg.find_orientations.orientation_maps.bin_frames
    eta_bins = np.int(2*np.pi / abs(reader.getDeltaOmega())) / bin_frames
    eta_ome = xrdutil.CollapseOmeEta(
        reader,
        pd,
        pd.hkls[:, active_hkls],
        detector,
        nframesLump=bin_frames,
        nEtaBins=eta_bins,
        debug=False,
        threshold=cfg.find_orientations.orientation_maps.threshold
        ).getEtaOmeMaps()

    fn = os.path.join(
        cfg.working_dir,
        cfg.find_orientations.orientation_maps.file
        )
    fd = os.path.split(fn)[0]
    if not os.path.isdir(fd):
        os.makedirs(fd)
    with open(fn, 'w') as f:
        cPickle.dump(eta_ome, f)
    logger.info("saved eta/ome orientation maps to %s", fn)
    return eta_ome


def find_orientations(cfg, hkls=None, clean=False, profile=False):
    """
    Takes a config dict as input, generally a yml document

    NOTE: single cfg instance, not iterator!
    """

    # a goofy call, could be replaced with two more targeted calls
    pd, reader, detector = initialize_experiment(cfg)

    # need instrument cfg later on down...
    instr_cfg = get_instrument_parameters(cfg)
    detector_params = np.hstack([
        instr_cfg['detector']['transform']['tilt_angles'],
        instr_cfg['detector']['transform']['t_vec_d'],
        instr_cfg['oscillation_stage']['chi'],
        instr_cfg['oscillation_stage']['t_vec_s'],
        ])
    rdim = cfg.instrument.detector.pixels.size[0]*cfg.instrument.detector.pixels.rows
    cdim = cfg.instrument.detector.pixels.size[1]*cfg.instrument.detector.pixels.columns
    panel_dims = ((-0.5*cdim, -0.5*rdim),
                  ( 0.5*cdim,  0.5*rdim),
                  )
    # UGH! hard-coded distortion...
    if instr_cfg['detector']['distortion']['function_name'] == 'GE_41RT':
        distortion = (dFuncs.GE_41RT,
                      instr_cfg['detector']['distortion']['parameters'],
                      )
    else:
        distortion = None

    # start logger
    logger.info("beginning analysis '%s'", cfg.analysis_name)

    # load the eta_ome orientation maps
    eta_ome = load_eta_ome_maps(cfg, pd, reader, detector, hkls=hkls, clean=clean)

    ome_range = (np.min(eta_ome.omeEdges),
                 np.max(eta_ome.omeEdges)
                 )
    try:
        # are we searching the full grid of orientation space?
        qgrid_f = cfg.find_orientations.use_quaternion_grid
        quats = np.loadtxt(qgrid_f).T
        logger.info("Using %s for full quaternion search", qgrid_f)
        hkl_ids = None
    except (IOError, ValueError):
        # or doing a seeded search?
        logger.info("Defaulting to seeded search")
        hkl_seeds = cfg.find_orientations.seed_search.hkl_seeds
        hkl_ids = [eta_ome.planeData.hklDataList[i]['hklID'] for i in hkl_seeds]
        hklseedstr = ', '.join(
            [str(i) for i in eta_ome.planeData.hkls.T[hkl_seeds]]
            )
        logger.info(
            "Seeding search using hkls from %s: %s",
            cfg.find_orientations.orientation_maps.file,
            hklseedstr
            )
        quats = generate_orientation_fibers(
            eta_ome,
            cfg.find_orientations.threshold,
            cfg.find_orientations.seed_search.hkl_seeds,
            cfg.find_orientations.seed_search.fiber_ndiv
            )
        if save_as_ascii:
            np.savetxt(
                os.path.join(cfg.working_dir, 'trial_orientations.dat'),
                quats.T,
                fmt="%.18e",
                delimiter="\t"
                )

    # generate the completion maps
    logger.info("Running paintgrid on %d trial orientations", (quats.shape[1]))
    if profile:
        logger.info("Profiling mode active, forcing ncpus to 1")
        ncpus = 1
    else:
        ncpus = cfg.multiprocessing
        logger.info(
            "%d of %d available processors requested", ncpus, mp.cpu_count()
            )
    compl = idx.paintGrid(
        quats,
        eta_ome,
        etaRange=np.radians(cfg.find_orientations.eta.range),
        omeTol=np.radians(cfg.find_orientations.omega.tolerance),
        etaTol=np.radians(cfg.find_orientations.eta.tolerance),
        omePeriod=np.radians(cfg.find_orientations.omega.period),
        threshold=cfg.find_orientations.threshold,
        doMultiProc=ncpus > 1,
        nCPUs=ncpus
        )

    if save_as_ascii:
        np.savetxt(os.path.join(cfg.working_dir, 'completeness.dat'), compl)
    else:
        np.save(os.path.join(cfg.working_dir, 'scored_orientations.npy'),
                np.vstack([quats, compl])
                )

    ##########################################################
    ##   Simulate N random grains to get neighborhood size  ##
    ##########################################################
    if hkl_ids is not None:
        ngrains = 100
        rand_q = mutil.unitVector(np.random.randn(4, ngrains))
        rand_e = np.tile(2.*np.arccos(rand_q[0, :]), (3, 1)) \
          * mutil.unitVector(rand_q[1:, :])
        refl_per_grain = np.zeros(ngrains)
        num_seed_refls = np.zeros(ngrains)
        for i in range(ngrains):
            grain_params = np.hstack([rand_e[:, i],
                                      xf.zeroVec.flatten(),
                                      xf.vInv_ref.flatten()
                                      ])

            eta_range = np.radians(cfg.find_orientations.eta.range)
            pixel_pitch = cfg.instrument.detector.pixels.size
            sim_results = xrdutil.simulateGVecs(pd,
                                                detector_params,
                                                grain_params,
                                                ome_range=(ome_range,),
                                                ome_period=(ome_range[0],
                                                            ome_range[0]+2*np.pi),
                                                eta_range=eta_range,
                                                panel_dims=panel_dims,
                                                pixel_pitch=pixel_pitch,
                                                distortion=distortion,
                                                )
            refl_per_grain[i] = len(sim_results[0])
            num_seed_refls[i] = np.sum([sum(sim_results[0] == hkl_id) for hkl_id in hkl_ids])
            pass
        
        cfg_completeness = cfg.find_orientations.clustering.completeness
        min_samples = max(cfg_completeness*np.floor(np.average(num_seed_refls)), 2)
        mean_rpg = int(np.round(np.average(refl_per_grain)))
    else:
        min_samples = 1
        mean_rpg = 1

    logger.info("mean number of reflections per grain is %d", mean_rpg)
    logger.info("neighborhood size estimate is %d points", min_samples)

    # cluster analysis to identify orientation blobs, the final output:
    qbar, cl = run_cluster(compl, quats, pd.getQSym(), cfg, min_samples=min_samples)

    np.savetxt(
        os.path.join(cfg.working_dir, 'accepted_orientations.dat'),
        qbar.T,
        fmt="%.18e",
        delimiter="\t"
        )
    return
