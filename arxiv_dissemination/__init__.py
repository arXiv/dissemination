"""Dissemination flask application"""
import os

from pathlib import Path
from functools import partial

from google.cloud import storage

from flask import Flask

from .routes import blueprint
from .trace import setup_trace

from .object_stores import to_obj_gs, to_obj_local, local_list, gs_list

import logging
logging.basicConfig(level=logging.INFO)


#################### config ####################
storage_prefix = os.environ.get('STORAGE_PREFIX','gs://arxiv-production-data')
"""Storage prefix to use. Ex gs://arxiv-production-data

If it is a GS bucket it must be just gs://{BUCKET_NAME} and not have
any key parts.

Use something like `/cache/` for a file system. Use something like
`./testing/data/` for testing data. Must end with a /
"""

trace = bool(os.environ.get('TRACE', '1') == '1')
"""To activate Google logging and trace.

On by default, set to 0 to deactivate.
"""

#################### App ####################
app = Flask(__name__)
app.config.update(
    storage_prefix=storage_prefix,
)
app.register_blueprint(blueprint)

############### trace and logging setup ###############
if trace:
    setup_trace(__name__,app)

app.logger.info(f"trace is {trace}")
app.logger.info(f"storage_prefix is {storage_prefix}")

problems = []
if not storage_prefix.startswith("gs://"):
    app.logger.warning(f"Using local files as object store at {storage_prefix}, Use this in testing only.")
    if not Path(storage_prefix).exists():
        problems.append(f"Directory {storage_prefix} does not exist.")
    if not storage_prefix.endswith('/'):
        problems.append(f'If using a local FS, STORAGE_PREFIX must end with a slash, was {storage_prefix}')
    setattr(app, 'get_obj_for_key', partial(to_obj_local, storage_prefix))
    setattr(app, 'list_blobs', partial(local_list, storage_prefix))
else:
    gs_client = storage.Client()
    bname= storage_prefix.replace('gs://','')
    if '/' in bname:
        problems.append(f"GS bucket should not have a key part, was {bname}")
    bucket = gs_client.bucket(bname)
    if not bucket.exists():
        problems.append(f"GS bucket {bucket} does not exist.")
    setattr(app, 'get_obj_for_key', partial(to_obj_gs, bucket))
    setattr(app, 'list_blobs', partial(gs_list, bucket))

if problems:
    [app.logger.error(prob) for prob in problems]
    exit(1)
