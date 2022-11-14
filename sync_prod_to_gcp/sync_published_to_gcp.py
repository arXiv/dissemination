"""Uplaods new and rep entries from a publish log.

ex

python sync_to_arxiv_produciton.py /data/new/logs/publish_221101.log

The PUBLISHLOG fiels can be found on the legacy FS at
/data/new/logs/publish_YYMMDD.log

This works by parsing the PUBLISHLOG file for new and rep entries,
those are put in the `todo_q` queue. Then for each of these
`arxiv_id`s it will check that the PDF file for the `arxiv_id` exists
in the `/data/ps_cache`. If it does not it will request the `arxiv_id`
via HTTP from the arxiv.org site. Once that returns the PDF will be
uploaded to the GS bucket.

"""

import sys

import re
import threading
from threading import Thread
from queue import Queue, Empty
import requests
from time import sleep, perf_counter
from datetime import datetime
import signal

from pathlib import Path

from identifier import Identifier

overall_start = perf_counter()

from google.cloud import storage

import logging
logging.basicConfig(level=logging.INFO, format='%(message)s (%(threadName)s)')
logger = logging.getLogger(__file__)
logger.setLevel(logging.INFO)

GS_BUCKET= 'arxiv-production-data'
GS_KEY_PREFIX = '/ps_cache'

FS_PREFIX= '/cache/ps_cache/'

ENSURE_UA = 'periodic-rebuild'
ENSURE_HOSTS = [ 'web2.arxiv.org', 'web3.arxiv.org', 'web4.arxiv.org', 'web5.arxiv.org', 'web6.arxiv.org', 'web7.arxiv.org', 'web8.arxiv.org', 'web9.arxiv.org', ]
ENSURE_CERT_VERIFY=False

THREADS = 16

new_r = re.compile(r"^.* new submission\n.* paper_id: (.*)$", re.MULTILINE)
rep_r = re.compile(r"^.* replacement for (.*)\n.*\n.* old version: (\d*)\n.* new version: (\d*)", re.MULTILINE)
# TODO handle wdr

todo_q = Queue()
uploaded_q = Queue() # number of files uploaded
global done
global run

run = True
done = False

# Ensure that ctrl-c works, mostly useful during testing
def handler_stop_signals(signum, frame):
    run = False

signal.signal(signal.SIGINT, handler_stop_signals)
signal.signal(signal.SIGTERM, handler_stop_signals)

def pdf_cache_path(arxiv_id):
    archive = ('arxiv' if not arxiv_id.is_old_id else arxiv_id.archive)
    format = 'pdf'
    return Path(f"{FS_PREFIX}/{archive}/{format}/{arxiv_id.yymm}/{arxiv_id.filename}v{arxiv_id.version}.pdf")

def pdf_src_path(arxiv_id, a_type):
    archive = ('arxiv' if not arxiv_id.is_old_id else arxiv_id.archive)
    format = 'pdf'
    if a_type == 'new':
        return Path(f"/data/ftp/{archive}/papers/{arxiv_id.yymm}/{arxiv_id.filename}.pdf")
    elif a_type == 'rep':
        return Path(f"/data/orig/{archive}/papers/{arxiv_id.yymm}/{arxiv_id.filename}v{arxiv_id.version}.pdf")
    elif a_type == 'prev':
        prev_v = int(arxiv_id.version) - 1
        return Path(f"/data/orig/{archive}/papers/{arxiv_id.yymm}/{arxiv_id.filename}v{prev_v}.pdf")
    else:
        return Path('/bogus/path')

def arxiv_pdf_url(host, arxiv_id):
    return f"https://{host}/pdf/{arxiv_id.filename}v{arxiv_id.version}.pdf"


def pdf_path_to_bucket_key(pdf):
    """Handels both source and cache files. Should handle pdfs, abs, txt
    and other types of files under these directories. Bucket key should
    not start with a /"""
    if str(pdf).startswith('/cache/'):
        return str(pdf).replace('/cache/','/')
    elif str(pdf).startswith('/data/'):
        return str(pdf).replace('/data/','/')

def is_src_pdf(arxiv_id):
    return pdf_src_path(arxiv_id, 'new').exists() or pdf_src_path(arxiv_id, 'rep').exists() or pdf_src_path(arxiv_id, 'pref').exists()

def ensure_pdf(session, host, arxiv_id, a_type):
    """Ensures PDF exits for arxiv_id.  

    Check both for source pdf and on the ps_cache.  If it does not
    exist, request it and wait for the PDF to be built.

    TODO Not sure if it is possible to have a paper that was a TeX source on version N but then is
    PDF Source on version N+1.

    Returns a list of Paths that should be synced to GCP.
    """    
    if is_src_pdf(arxiv_id):
        if a_type == 'new':
            logger.info(f"{arxiv_id.filename} is PDF src and new")
            return [pdf_src_path(arxiv_id, a_type)]
        else:
            logger.info(f"{arxiv_id.filename} is PDF src and rep")
            # need to replace the file in /ftp and add a version to /orig
            return [pdf_src_path(arxiv_id, 'new'), pdf_src_path(arxiv_id, 'prev')]
    else:
        logger.info(f"{arxiv_id.filename} is not PDF src")
        return [ensure_file_url_exists(session, host, pdf_cache_path(arxiv_id), arxiv_pdf_url(host, arxiv_id))]


def ensure_file_url_exists(session, host, pdf_file, url):
    """General purpose ensure exits for a `url` that should produce a `pdf_file`."""
    if not pdf_file.exists():
        start = perf_counter()
        headers = { 'User-Agent': ENSURE_UA }
        resp = session.get(url, headers=headers, stream=True, verify=ENSURE_CERT_VERIFY)
        [line for line in resp.iter_lines()]  # Consume resp in hopes of keeping alive session
        logger.info(f"ensure_file_url_exists: built {str(pdf_file)} {int((perf_counter()-start)*1000)} ms {url} status_code {resp.status_code}")
        sleep(5)
    else:
        logger.info(f"ensure_file_url_exists: {str(pdf_file)} already exists")
    return pdf_file


def upload_pdf(gs_client, pdf):
    """Uploads pdf to GS_BUCKET"""
    bucket = gs_client.bucket(GS_BUCKET)
    key = pdf_path_to_bucket_key(pdf)
    blob = bucket.get_blob(key)
    if blob is None or blob.size != pdf.stat().st_size:
        with open(pdf, 'rb') as fh:
            bucket.blob(key).upload_from_file(fh, content_type='application/pdf')
        uploaded_q.put(pdf.stat().st_size)
        logger.info(f"upload: completed upload of {pdf} to gs://{GS_BUCKET}/{key} of size {pdf.stat().st_size}")
    else:
        logger.info(f"upload: Not uploading {pdf}, gs://{GS_BUCKET}/{key} already on gs")

    
def sync_to_gcp(todo_q, host):
    tl_data=threading.local()
    tl_data.session = requests.Session() # cannot share Session across threads
    tl_data.gs_client = storage.Client()

    while run:
        try:
            start = perf_counter()
            a_type, a_id, _, v_new = todo_q.get(block=False)
            arxiv_id = Identifier(f"{a_id}v{v_new}")
            pdf_paths = ensure_pdf(tl_data.session, host, arxiv_id, a_type)

            if pdf_paths:
                [upload_pdf(tl_data.gs_client, pdf) for pdf in pdf_paths if pdf is not None]
            else:
                logger.error("No PDF found for {a_id}v{v_new}")

            todo_q.task_done()
            logger.debug(f"Total time for {a_id}v{v_new} {int((perf_counter()-start)*1000)}ms")
        except Empty:
            break
        except Exception:
            logger.exception(f"Problem during {a_id}v{v_new}")

if __name__ == "__main__":
    if not len(sys.argv) > 1:
        print(sys.modules[__name__].__doc__)
        exit(1)

    _test_auth_client= storage.Client() #will fail if no auth setup
    logger.info(f"Starting at {datetime.now().isoformat()}")

    with open(sys.argv[1]) as fh:
        log = fh.read()
        for idx, m in enumerate(new_r.finditer(log)):
            todo_q.put( ('new', m.group(1), None, 1))
        for m in rep_r.finditer(log):
            todo_q.put( ('rep', m.group(1), m.group(2), m.group(3)))

    overall_size = todo_q.qsize()

    threads = []
    for idx in range(0, THREADS):
        t = Thread(target=sync_to_gcp, args=(todo_q, ENSURE_HOSTS[ idx % len(ENSURE_HOSTS) ]))
        threads.append(t)
        t.start()

    while run and not todo_q.empty():
        sleep( 0.1)

    done=True
    run=False
    [th.join() for th in threads]

    logger.info(f"Done at {datetime.now().isoformat()}")
    logger.info(f"Overall time: {(perf_counter()-overall_start):.2f} for {overall_size} submissions of type new or rep, {uploaded_q.qsize()} uploads.")
    if overall_size < uploaded_q.qsize():
        logger.info("Uploaded count maybe higher than submission count due to replacements needing to upload both to /ftp and /orig for papers with PDF source.")
    if overall_size > uploaded_q.qsize():
        logger.info("Uploaded count maybe lower than submission count due to files already synced to GCP.")
