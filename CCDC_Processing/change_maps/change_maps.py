"""
Change Maps for CCDC visualizations
"""

import os, sys
import datetime
import logging
import multiprocessing as mp
from optparse import OptionParser 
from osgeo import gdal, osr
import numpy as np
import scipy.io as sio


LOGGER = logging.getLogger()
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s [%(levelname)s]: %(message)s')
handler.setFormatter(formatter)
LOGGER.addHandler(handler)
LOGGER.setLevel(logging.DEBUG)


class ChangeMap(object):
    map_names = ('ChangeMap', 'ChangeMagMap', 'ConditionMap',
                 'NumberMap', 'QAMap')

    def __init__(self):
        self.rec_cg = None
        self.changemaps = None

    def create_changemap_dict(self, input_file):
        rec_cg = self.read_matlab_record(input_file)

        offy = int(os.path.split(input_file)[-1][13:-4]) - 1
        # LOGGER.debug('Starting on line: {0}'.format(offy))

        self.changemaps = {'y_off': offy,
                           'ChangeMap': {},
                           'ChangeMagMap': {},
                           # 'CoverMap': np.zeros((1, 1), dtype=np.int32),
                           # 'CoverQAMap': np.zeros((1, 1), dtype=np.int32),
                           'ConditionMap': {},
                           'QAMap': {},
                           'NumberMap': {}
                           }

        pos = rec_cg['pos']
        t_start = rec_cg['t_start']
        t_end = rec_cg['t_end']
        t_break = rec_cg['t_break']
        coefs = rec_cg['coefs']
        change_prob = rec_cg['change_prob']
        categ = rec_cg['category']

        mag = rec_cg['magnitude']
        number = rec_cg['num_obs']

        for idx in range(len(pos)):
            arr_pos = (pos[idx] - 1) % 5000

            start_dt = self.matlab2datetime(t_start[idx])
            end_dt = self.matlab2datetime(t_end[idx])

            b_start = coefs[idx][0, 0] + t_start[idx] * coefs[idx][0, 1]
            r_start = coefs[idx][2, 0] + t_start[idx] * coefs[idx][2, 1]
            n_start = coefs[idx][3, 0] + t_start[idx] * coefs[idx][3, 1]

            b_end = coefs[idx][0, 0] + t_end[idx] * coefs[idx][0, 1]
            r_end = coefs[idx][2, 0] + t_end[idx] * coefs[idx][2, 1]
            n_end = coefs[idx][3, 0] + t_end[idx] * coefs[idx][3, 1]

            evi_start = 2.5 * (n_start - r_start) / (n_start + 6 * r_start - 7.5 * b_start + 10000)
            evi_end = 2.5 * (n_end - r_end) / (n_end + 6 * r_end - 7.5 * b_end + 10000)

            evi_slope = 10000 * (evi_end - evi_start) / (t_end[idx] - t_start[idx])

            for yr in range(start_dt.year, end_dt.year + 1):
                self.add_year(yr)

                self.changemaps['ConditionMap'][yr][arr_pos] = evi_slope
                self.changemaps['QAMap'][yr][arr_pos] = categ[idx]
                self.changemaps['NumberMap'][yr][arr_pos] = number[idx]

            if pos[idx] == pos[idx - 1]:
                self.changemaps['ChangeMap'][start_dt.year][arr_pos] = start_dt.timetuple().tm_yday
                self.changemaps['ChangeMagMap'][start_dt.year][arr_pos] = np.linalg.norm(mag[idx], ord=2)

                # Classification
                # prod_files['CoverMap'][out_idx, year_idx] = ccdc_class[locs][0]
                # prod_files['CoverQAMap'][out_idx, year_idx] = class_qa[locs][0]

        return self.changemaps

    def add_year(self, year):
        for c in self.map_names:
            if year not in self.changemaps[c]:
                self.changemaps[c][year] = np.zeros((5000, 1))

    @staticmethod
    def read_matlab_record(file_path):
        return sio.loadmat(file_path, squeeze_me=True)['rec_cg']

    @staticmethod
    def matlab2datetime(matlab_datenum):
        day = datetime.datetime.fromordinal(int(matlab_datenum))
        dayfrac = datetime.timedelta(days=matlab_datenum % 1) - datetime.timedelta(days=366)
        return day + dayfrac


def output_maps(data, output_dir, ref_image):
    y_off = data.pop('y_off')

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    for prod in data:
        for year in data[prod]:
            ds = get_raster_ds(output_dir, prod, year, ref_image)
            ds.GetRasterBand(1).WriteArray(data[prod][year].reshape(1, 5000), 0, y_off)

            ds.FlushCache()
            ds = None


def get_raster_info(ref_image):
    ds = gdal.Open(ref_image, gdal.GA_ReadOnly)

    geo = ds.GetGeoTransform()
    proj = ds.GetProjection()
    rows = ds.RasterYSize
    cols = ds.RasterXSize

    ds = None

    return geo, proj, rows, cols


def get_raster_ds(output_dir, product, year, ref_image):
    key = '{0}_{1}'.format(product, year)

    file_path = os.path.join(output_dir, key + '.tif')

    if os.path.exists(file_path):
        ds = gdal.Open(file_path, gdal.GA_Update)
    else:
        ds = create_geotif(file_path, product, ref_image)

    return ds


def create_geotif(file_path, product, ref_image):
    data_type = prod_data_type(product)
    geo, proj, rows, cols = get_raster_info(ref_image)

    ds = (gdal
          .GetDriverByName('GTiff')
          .Create(file_path, cols, rows, 1, data_type))

    ds.SetGeoTransform(geo)
    ds.SetProjection(proj)

    return ds


def prod_data_type(product):
    if product in ('ChangeMap', 'NumberMap'):
        return gdal.GDT_UInt16
    elif product in ('ChangeMagMap', 'ConditionMap'):
        return gdal.GDT_Float32
    elif product in ('CoverMap', 'CoverQAMap', 'QAMap'):
        return gdal.GDT_Byte
    else:
        raise ValueError


def multi_output(output_dir, ref_image, output_q, kill_count):
    count = 0
    while True:
        if count >= kill_count:
            break

        outdata = output_q.get()

        if outdata == 'kill':
            count += 1
            continue

        LOGGER.debug('Outputting line: {0}'.format(outdata['y_off']))
        output_maps(outdata, output_dir, ref_image)


def multi_worker(input_q, output_q):
    while True:
        infile = input_q.get()

        if infile == 'kill':
            output_q.put('kill')
            break

        change = ChangeMap().create_changemap_dict(infile)

        output_q.put(change)


def single_run(input_dir, output_dir, ref_image):
    for f in os.listdir(input_dir):
        change = ChangeMap().create_changemap_dict(os.path.join(input_dir, f))
        LOGGER.debug('Outputting line: {0}'.format(change['y_off']))
        output_maps(change, output_dir, ref_image)


def multi_run(input_dir, output_dir, ref_image, num_procs):
    input_q = mp.Queue()
    output_q = mp.Queue()

    worker_count = num_procs - 1

    for f in os.listdir(input_dir):
        input_q.put(os.path.join(input_dir, f))

    for _ in range(worker_count):
        input_q.put('kill')

    for _ in range(worker_count):
        mp.Process(target=multi_worker, args=(input_q, output_q)).start()

    multi_output(output_dir, ref_image, output_q, worker_count)
    
def main():
	parser = OptionParser()

   # define options
	parser.add_option("-i", dest="in Dir", help="(Required) Location of input data")
	parser.add_option("-o", dest="out Dir", help="(Required) Location of output data to be saved")
	parser.add_option("-r", dest="Test Image", help="(Required) reference image")
    	parser.add_option("-n", dest="num process", default = 4, help="number of processes")
	
	(ops, arg) = parser.parse_args()

	if len(arg) == 1:
		parser.print_help()
	elif not ops.in_Dir:
		parser.print_help()
		sys.exit(1)
	elif not ops.out_Dir:
		parser.print_help()
		sys.exit(1)
    	elif not ops.Test_Image:
		parser.print_help()
		sys.exit(1)

	else:
		multi_run(ops.in_Dir, ops.out_Dir, ops.Test_Image, ops.num_process)  

if __name__ == '__main__':
    main()
   ## indir = r'D:\lcmap\matlab_compare\WA-08\zhe\TSFitMap'
   ## outdir = r'D:\lcmap\matlab_compare\WA-08\klsmith\changemaps'

   ## test_image = r'D:\lcmap\matlab_compare\WA-08\LT50460271990297\LT50460271990297PAC04_MTLstack'
    ### single_run(indir, outdir, test_image)
   ## multi_run(indir, outdir, test_image, 4)
