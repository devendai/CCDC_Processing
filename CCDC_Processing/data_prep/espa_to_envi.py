import os
import subprocess
import multiprocessing as mp
import tarfile
import shutil
import logging
import sys
from optparse import OptionParser 
from ard_filter import ARDFiltering
from ard_filters import Fill_10percent, Fill_20percent, NoFill_10percent, NoFill_20percent


WORK_DIR = '/dev/shm'

# GDAL_PATH = os.environ.get('GDAL')
# if not GDAL_PATH:
#     raise Exception('GDAL environment variable not set')
#
# GDAL_PATH = os.path.join(GDAL_PATH, 'bin')

LOGGER = logging.getLogger()
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s [%(levelname)s]: %(message)s')
handler.setFormatter(formatter)
LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)


def create_tiles(inpath, outpath, worker_num):
    if not os.path.exists(outpath):
        os.makedirs(outpath)

    file_q = mp.Queue()
    message_q = mp.Queue()

    file_enqueue(inpath, file_q, worker_num)
    work = work_paths(worker_num, WORK_DIR)

    message = mp.Process(target=progress, args=(message_q, worker_num))
    message.start()
    for i in range(worker_num - 1):
        p_args = (file_q, message_q, outpath, work[i])
        mp.Process(target=process_tile, args=p_args).start()

    message.join()


def process_tile(file_q, prog_q, out_path, work_path):
    """Process a file from the queue"""
    def unpackage():
        with tarfile.open(file) as f:
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(f, path=work_path)

    def translate():
        subprocess.call('gdal_translate -of ENVI -co "INTERLEAVE=BIP" {} {}'
                        .format(pathing['TRAN']['IN'], pathing['TRAN']['OUT']), shell=True)

    def vrt():
        subprocess.call('gdalbuildvrt -separate {} {}'
                        .format(pathing['VRT']['OUT'], pathing['VRT']['IN']), shell=True)

    def build_paths():
        base = os.path.join(out_path, tiff_base)

        if not os.path.exists(base):
            os.makedirs(base)

        phs = {'VRT': {'OUT': os.path.join(work_path, tiff_base + '.vrt'),
                       'IN': ' '.join(band_list)},
               'TRAN': {'IN': os.path.join(work_path, tiff_base + '.vrt'),
                        'OUT': os.path.join(base, tiff_base + '_MTLstack')},
               'GCP': {'IN': os.path.join(work_path, tiff_base + '_GCP.txt'),
                       'OUT': os.path.join(base, tiff_base + '_GCP.txt')},
               'MTL': {'IN': os.path.join(work_path, tiff_base + '_MTL.txt'),
                       'OUT': os.path.join(base, tiff_base + '_MTL.txt')}}
        return phs

    def build_l8_list():
        return ['{}_sr_band2.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band3.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band4.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band5.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band6.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band7.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_toa_band10.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_cfmask.tif'.format(os.path.join(work_path, tiff_base))]

    def build_tm_list():
        return ['{}_sr_band1.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band2.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band3.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band4.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band5.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_sr_band7.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_toa_band6.tif'.format(os.path.join(work_path, tiff_base)),
                '{}_cfmask.tif'.format(os.path.join(work_path, tiff_base))]

    def base_name():
        base = ''
        for tiff_file in os.listdir(work_path):
            if tiff_file[-12:] != 'sr_band1.tif':
                continue

            base = tiff_file[:21]
            break

        return base

    def clean_up():
        for f in os.listdir(work_path):
            os.remove(os.path.join(work_path, f))

    proc = work_path[-1]
    filters = [Fill_20percent, Fill_10percent, NoFill_20percent, NoFill_10percent]
    while True:
        try:
            file = file_q.get()

            if file == 'KILL':
                prog_q.put('Killing process %s' % proc)
                break

            prog_q.put('Process %s: Unpacking %s' % (proc, file))
            unpackage()

            tiff_base = base_name()

            if tiff_base[2] == '8':
                band_list = build_l8_list()
            else:
                band_list = build_tm_list()

            pathing = build_paths()

            if os.path.exists(pathing['TRAN']['OUT']):
                clean_up()
                continue

            prog_q.put('Process %s: Building VRT stack for %s' % (proc, tiff_base))
            vrt()

            prog_q.put('Process %s: Calling Translate for %s' % (proc, tiff_base))
            translate()

            prog_q.put('Process %s: Moving ancillery files for %s' % (proc, tiff_base))
            if os.path.exists(pathing['GCP']['IN']):
                shutil.copy(pathing['GCP']['IN'], pathing['GCP']['OUT'])
            if os.path.exists(pathing['MTL']['IN']):
                shutil.copy(pathing['MTL']['IN'], pathing['MTL']['OUT'])

            with ARDFiltering(os.path.join(out_path, 'filtered'), filters) as f:
                f.filter(pathing['TRAN']['OUT'])

            clean_up()
        except Exception as e:
            prog_q.put('Process %s: Hit an exception - %s' % (proc, e))
            prog_q.put('Killing process %s' % proc)
            clean_up()
            break

    os.rmdir(work_path)


def file_enqueue(path, q, worker_num):
    """Builds a queue of files to be processed"""

    for gzfile in os.listdir(path):
        if gzfile[-2:] != 'gz':
            continue

        q.put(os.path.join(path, gzfile))

    for _ in range(worker_num):
        q.put('KILL')


def work_paths(worker_num, path):
    """Makes working directories for the multi-processing"""

    out = []
    for i in range(worker_num - 1):
        new_path = os.path.join(path, 'espa_ard_working%s' % i)
        out.append(new_path)
        if not os.path.exists(new_path):
            os.mkdir(new_path)

    return out


def progress(prog_q, worker_num):
    count = 0
    while True:
        message = prog_q.get()

        # print(message)
        # sys.stdout.write(message)
        LOGGER.info(message)

        if message[:4] == 'Kill':
            count += 1
        if count >= worker_num - 1:
            break

def main():
	parser = OptionParser()
   # define options
	parser.add_option("-i", dest="in_Dir", help="(Required) Location of input data")
	parser.add_option("-o", dest="out_Dir", help="(Required) Location of output data to be saved")
	parser.add_option("-n", dest="num_worker", default = 20, help="number of workder")
	
	(ops, arg) = parser.parse_args()

	if len(arg) == 1:
		parser.print_help()
	elif not ops.in_Dir:
		parser.print_help()
		sys.exit(1)
	elif not ops.out_Dir:
		parser.print_help()
		sys.exit(1)

	else:
		create_tiles(ops.in_Dir, ops.out_Dir, ops.num_worker)   

if __name__ == '__main__':
    main()
    # input_path = raw_input('Tarball inputs: ')
    # output_path = raw_input('Output directory: ')
    # num = raw_input('Number of workers: ')
    '''
    if len(sys.argv) > 1:
        create_tiles(sys.argv[1], sys.argv[2], int(sys.argv[3]))

    input_path = '/shared/users/klsmith/klsmith@usgs.gov-06142016-094545'
    output_path = '/shared/users/klsmith/AL-h07v09'
    num = 20
    
    create_tiles(input_path, output_path, int(num))
    '''
