"""
Using the pcmin function, and data from the ERA5 reanalysis project,
calculate gridded potential intensity values.
"""

import os
import sys
import pdb
import logging
import argparse
import datetime
import cftime
from calendar import monthrange
from time import sleep
from configparser import ConfigParser
from os.path import join as pjoin, realpath, isdir, dirname, splitext

import numpy as np
from netCDF4 import Dataset

import metutils
import nctools
from pcmin import pcmin
from parallel import attemptParallel, disableOnWorkers

LOGGER = logging.getLogger()


def main():
    """
    Handle command line arguments and call processing functions

    """
    p = argparse.ArgumentParser()

    p.add_argument('-c', '--config_file', help="Configuration file")
    p.add_argument('-v', '--verbose',
                   help="Verbose output", 
                   action='store_true')
    args = p.parse_args()

    configFile = args.config_file
    config = ConfigParser()
    config.read(configFile)

    logFile = config.get('Logging', 'LogFile')
    logdir = dirname(realpath(logFile))

    # if log file directory does not exist, create it
    if not isdir(logdir):
        try:
            os.makedirs(logdir)
        except OSError:
            logFile = pjoin(os.getcwd(), 'pcmin.log')



    logLevel = config.get('Logging', 'LogLevel')
    verbose = config.getboolean('Logging', 'Verbose')
    datestamp = config.getboolean('Logging', 'Datestamp')
    if args.verbose:
        verbose = True
    if comm.size > 1 and comm.rank > 0:
        logFile += '-' + str(comm.rank)
        verbose = False


    if datestamp:
        base, ext = splitext(logFile)
        curdate = datetime.datetime.now()
        curdatestr = curdate.strftime('%Y%m%d%H%M')
        logfile = f"{base}.{curdatestr}.{ext.lstrip('.')}"

    logging.basicConfig(level=logLevel, 
                        format="%(asctime)s: %(funcName)s: %(message)s",
                        filename=logfile, filemode='w',
                        datefmt="%Y-%m-%d %H:%M:%S")

    if verbose:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(getattr(logging, logLevel))
        formatter = logging.Formatter('%(asctime)s: %(funcName)s:  %(message)s',
                                      '%H:%M:%S', )
        console.setFormatter(formatter)
        LOGGER.addHandler(console)

    LOGGER.info(f"Started {sys.argv[0]} (pid {os.getpid()})")
    LOGGER.info(f"Log file: {logfile} (detail level {logLevel})")

    year = 2019
    month = 12
    LOGGER.info(f"Processing {year}-{month}")
    startdate = datetime.datetime(year, month, 1)
    enddate = datetime.datetime(year, month, monthrange(year, month)[1])

    filedatestr = f"{startdate.strftime('%Y%m%d')}_{enddate.strftime('%Y%m%d')}"

    tpath = config.get('Input', 'Temp')
    tfile = pjoin(tpath, f'{year}', f'T_era5_aus_{filedatestr}.nc')
    tobj = nctools.ncLoadFile(tfile)
    tvar = nctools.ncGetVar(tobj, 't')
    tvar.set_auto_maskandscale(True)
    rpath = config.get('Input', 'Humidity')
    rfile = pjoin(rpath, f'{year}', f'R_era5_aus_{filedatestr}.nc')
    robj = nctools.ncLoadFile(rfile)
    rvar = nctools.ncGetVar(robj, 'r')
    rvar.set_auto_maskandscale(True)
    # This is actually relative humidity, we need to convert to mixing ratio
    # Calculate mixing ratio - this function returns mixing ratio in g/kg

    # Dimensions need to come from the pressure files
    # These have been clipped to the Australian region, so contain
    # a subset of the global data. The SST and MSLP data
    # are then clipped to the same domain
    tlon = nctools.ncGetDims(tobj, 'longitude')
    tlat = nctools.ncGetDims(tobj, 'latitude')


    LOGGER.info(f"Loading SST data")
    sstpath = config.get('Input', 'SST')
    sstfile = pjoin(sstpath, f'{year}', f'SSTK_era5_global_{filedatestr}.nc' )
    sstobj = nctools.ncLoadFile(sstfile)
    sstvar = nctools.ncGetVar(sstobj,'sst')
    sstvar.set_auto_maskandscale(True)
    sstlon = nctools.ncGetDims(sstobj, 'longitude')
    sstlat = nctools.ncGetDims(sstobj, 'latitude')

    LOGGER.info("Loading SLP data")
    slppath = config.get('Input', 'SLP')
    slpfile = pjoin(slppath, f'{year}', f'MSL_era5_global_{filedatestr}.nc')
    slpobj = nctools.ncLoadFile(slpfile)
    slpvar = nctools.ncGetVar(slpobj, 'msl')
    slpvar.set_auto_maskandscale(True)

    # In the ERA5 data on NCI, surface variables are global, 
    # pressure variables are only over Australian region
    LOGGER.info("Getting intersection of grids")
    lonx, sstidx, varidx = np.intersect1d(sstlon, tlon, return_indices=True)
    laty, sstidy, varidy = np.intersect1d(sstlat, tlat, return_indices=True)

    nx = len(varidx)
    ny = len(varidy)
    LOGGER.info("Loading and converting SST and SLP data")
    sst = metutils.convert(sstvar[:, sstidy, sstidx], sstvar.units, 'C')
    slp = metutils.convert(slpvar[:, sstidy, sstidx], slpvar.units, 'hPa')


    times = nctools.ncGetTimes(nctools.ncLoadFile(tfile))
    nt = len(times)
    LOGGER.debug(f"There are {nt} times in the data file")

    levels = nctools.ncGetDims(nctools.ncLoadFile(tfile), 'level')
    nz = len(levels)
    LOGGER.debug(f"There are {nz} vertical levels in the data file")

    # Create an array of the pressure variable that 
    # matches the shape of the temperature and mixing ratio
    # variables.
    LOGGER.info("Creating temporary pressure array")
    pp = np.ones((nz, ny, nx))
    ppT = pp.T
    ppT *= levels

    pmin = np.zeros(sst.shape)
    vmax = np.zeros(sst.shape)

    status = MPI.Status()
    work_tag = 0
    result_tag = 1
    LOGGER.info("Calculating potential intensity")
    if (comm.rank == 0) and (comm.size > 1):
        w = 0
        p = comm.size - 1
        for d in range(1, comm.size):
            if w < nt:
                LOGGER.debug(f"Sending time {w} to node {d}")
                comm.send(w, dest=d, tag=work_tag)
                w += 1
            else:
                comm.send(None, dest=d, tag=work_tag)
                p = w

        terminated = 0
        while(terminated < p):
            result, tdx = comm.recv(source=MPI.ANY_SOURCE, status=status, tag=MPI.ANY_TAG)
            pmin[tdx, :, :], vmax[tdx, :, :] = result
            d = status.source

            if w < nt:
                LOGGER.debug(f"Sending time {w} to node {d}")
                comm.send(w, dest=d, tag=status.tag)
                w += 1
            else:
                comm.send(None, dest=d, tag=status.tag)
                terminated += 1
    elif (comm.size > 1) and (comm.rank != 0):
        status = MPI.Status()
        W = None
        while(True):
            W = comm.recv(source=0, tag=work_tag, status=status)
            if W is None:
                LOGGER.debug("No work to be done on this processor: {0}".format(comm.rank))
                break
            LOGGER.debug(f"Processing time {times[W]} on node {comm.rank}")
            t = metutils.convert(tvar[W, :, varidy, varidx], tvar.units, 'C')
            r = metutils.rHToMixRat(rvar[W, :, varidy, varidx], t, pp, 'C')
            r = np.where(r < 0, 0, r)
            results = calculate(sst[W,:,:], slp[W, :, :], pp, t, r, levels)
            LOGGER.debug(f"Finished time {times[W]} on node {comm.rank}")
            comm.send((results, W), dest=0, tag=status.tag)


    if comm.rank == 0:
        sleep(5)
    comm.Barrier()
    LOGGER.info("Saving data")
    outputPath = config.get('Output', 'Path')
    try:
        os.makedirs(outputPath)
    except:
        pass
    outputFile = pjoin(outputPath, 'pcmin.nc')
    saveData(outputFile, pmin, vmax, lonx, laty, times)

    LOGGER.info("Finished calculating potential intensity")




def calculate(sst, slp, pp, tt, rr, levels):
    ny, nx = sst.shape
    pmin = np.zeros(sst.shape)
    vmax = np.zeros(sst.shape)
    for jj in range(ny):
        for ii in range(nx):
            pmin[jj, ii], vmax[jj, ii], ifl = pcmin(sst[jj, ii],
                                                    slp[jj, ii],
                                                    pp[:, jj, ii],
                                                    tt[:, jj, ii],
                                                    rr[:,jj, ii],
                                                    len(levels),
                                                    len(levels))
    return pmin, vmax

@disableOnWorkers
def saveData(outputFile, pmin, vmax, lon, lat, times):
    LOGGER.info(f"Saving PI data to {outputFile}")
    dimensions = {
            0: {
                'name': 'time',
                'values': cftime.date2num(times, units='hours since 1900-01-01 00:00:00.0', calendar='gregorian'),
                'dtype': 'float',
                'atts': {
                    'long_name': 'time',
                    'units': 'hours since 1900-01-01 00:00:00.0',
                    'calendar': 'gregorian',
                    'axis': 'T'
                }
            },
            1: {
                'name': 'lat',
                'values': lat,
                'dtype': 'float64',
                'atts': {
                    'long_name': 'Latitude',
                    'standard_name': 'latitude',
                    'units': 'degrees_north',
                    'axis': 'Y'
                }
            },
            2: {
                'name': 'lon',
                'values': lon,
                'dtype': 'float64',
                'atts': {
                    'long_name': 'Longitude',
                    'standard_name': 'longitude',
                    'units': 'degrees_east',
                    'axis': 'X'
                }
            }
        }
    variables = {
            0: {
                'name': 'pmin',
                'dims': ('time', 'lat', 'lon'),
                'values': pmin,
                'dtype': 'float64',
                'atts': {
                    'long_name': 'minimum central pressure',
                    'standard_name': 'air_pressure_at_mean_sea_level',
                    'units': 'hPa',
                    'valid_range': (800., 1040.)
                }
            },
            1: {
                'name': 'vmax',
                'dims': ('time', 'lat', 'lon'),
                'values': vmax,
                'dtype': 'float64',
                'atts': {
                    'long_name': 'maximum sustained windspeed',
                    'standard_name': 'wind_speed',
                    'units': "m s**-1",
                    'valid_range': (0., 200.)
                }
            }
        }

    history = (f"Maximum potential intensity calculated using Emanuel's algorithm "
               f"and ERA5 reanalysis data for the Australian region ")
               
    gatts = {
        'history': history,
    }

    nctools.ncSaveGrid(outputFile, dimensions, variables, nodata=-9999,
            datatitle='Maximum potential intensity', gatts=gatts,
            writedata=True, keepfileopen=False, zlib=True, 
            complevel=4, lsd=None)
    return

if __name__ == "__main__":
    from parallel import attemptParallel, disableOnWorkers
    global MPI, comm
    MPI = attemptParallel()
    comm = MPI.COMM_WORLD
    main()
    