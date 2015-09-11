##########################################################################
#
#   MRC FGU Computational Genomics Group
#
#   $Id$
#
#   Copyright (C) 2009 Andreas Heger
#
#   This program is free software; you can redistribute it and/or
#   modify it under the terms of the GNU General Public License
#   as published by the Free Software Foundation; either version 2
#   of the License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
##########################################################################
'''Pipeline.py - Tools for ruffus pipelines
===========================================

The :mod:`Pipeline` module contains various utility functions for
interfacing CGAT ruffus pipelines with an HPC cluster, uploading data
to databases, providing parameterization, and more.

It is a collection of utility functions covering the topics:

* `Pipeline control`_
* `Logging`_
* `Parameterisation`_
* `Running tasks`_
* `Database upload`_
* `Report building`_

See :doc:`pipelines/pipeline_template` for a pipeline illustrating the
use of this module. See :ref:`PipelineSettingUp` on how to set up a
pipeline.

Pipeline control
----------------

:mod:`Pipeline` provides a :func:`main` function that provides command
line control to a pipeline. To use it, add::

    import CGAT.Pipeline as P
    # ...

    if __name__ == "__main__":
        sys.exit(P.main(sys.argv))

to your pipeline script. Typing::

    python my_pipeline.py --help

will provide the following output:

.. program-output:: python ../CGATPipelines/pipeline_template.py --help

Documentation on using pipelines is at :ref:`PipelineRunning`.

The functions :func:`writeConfigFiles`, :func:`clean`,
:func:`clonePipeline` and :func:`peekParameters` provide the
functionality for particular pipeline commands.

Logging
-------

Logging is set up by :func:`main`. Logging messages will be
sent to the file :file:`pipeline.log` in the current directory.

:class:`MultiLineFormatter` improves the formatting of long log
messages, while :class:`LoggingFilterRabbitMQ` intercepts ruffus log
messages and sends event information to a rabbitMQ message exchange
for task process monitoring.

Running tasks
-------------

:mod:`Pipeline` provides a :func:`Pipeline.run` method to control
running commandline tools. The :func:`Pipeline.run` method takes care
of distributing these tasks to the cluster. It takes into
consideration command line options such as ``--cluster-queue``. The
command line option ``--local`` will run jobs locally for testing
purposes.

For running Python code that is inside a module in a distributed
function, use the :func:`submit` function. The :func:`execute` method
runs a command locally.

Functions such as :func:`shellquote`, :func:`getCallerLocals`,
:func:`getCaller`, :func:`buildStatement`, :func:`expandStatement`,
:func:`joinStatements` support the parameter interpolation mechanism
used in :mod:`Pipeline`.

Parameterisation
----------------

:mod:`Pipeline` provides hooks for reading pipeline configuration
values from :file:`.ini` files and making them available inside ruffus_
tasks. The fundamental usage is a call to :func:`getParamaters` with
a list of configuration files, typically::

    PARAMS = P.getParameters(
        ["%s/pipeline.ini" % os.path.splitext(__file__)[0],
         "../pipeline.ini",
         "pipeline.ini"])

The :mod:`Pipeline` module defines a global variable :data:`PARAMS`
that provides access the configuration values.

Functions such as :func:`configToDictionary`, :func:`loadParameters`
:func:`matchParameter`, :func:`substituteParameters` support this
functionality.

Functions such as :func:`asList` and :func:`isTrue` are useful to work
with parameters.

The method :func:`peekParameters` allows one to programmatically read the
parameters of another pipeline.

Temporary files
---------------

Tasks containg multiple steps often require temporary memory storage
locations.  The functions :func:`getTempFilename`, :func:`getTempFile`
and :func:`getTempDir` provide these. These functions are aware of the
temporary storage locations either specified in configuration files or
on the command line and distinguish between the ``private`` locations
that are visible only within a particular compute node, and ``shared``
locations that are visible between compute nodes and typically on a
network mounted location.

Requirements
------------

The methods :func:`checkExecutables`, :func:`checkScripts` and
:func:`checkParameter` check for the presence of executables, scripts
or parameters. These methods are useful to perform pre-run checks
inside a pipeline if a particular requirement is met. But see also the
``check`` commandline command.

Database upload
---------------

To assist with uploading data into a database, :mod:`Pipeline` provides
several utility functions for conveniently uploading data. The :func:`load`
method uploads data in a tab-separated file::

    @transform("*.tsv.gz", suffix(".tsv.gz"), ".load")
    def loadData(infile, outfile):
        P.load(infile, outfile)

The methods :func:`mergeAndLoad` and :func:`concatenateAndLoad` upload
multiple files into same database by combining them first. The method
:func:`createView` creates a table or view derived from other tables
in the database.

The functions :func:`tablequote` and :func:`toTable` translates track
names derived from filenames into names that are suitable for tables.

The method :func:`build_load_statement` can be used to create an
upload command that can be added to command line statements to
directly upload data without storing an intermediate file.

The method :func:`connect` returns a database handle for querying the
database.

Report building
---------------

:func:`publish_notebooks`
:func:`isTest`.
:class:`MultiLineFormatter`
:class:`LoggingFilterRabbitMQ`

Reference
---------

'''
import os
import sys
import re
import subprocess
import collections
import stat
import tempfile
import time
import inspect
import types
import logging
import shutil
import pipes
import ConfigParser
import pickle
import importlib
from cStringIO import StringIO
import json
try:
    import pika
    HAS_PIKA = True
except ImportError:
    HAS_PIKA = False

from CGAT import Database as Database

# talking to a cluster
try:
    import drmaa
    HAS_DRMAA = True
except RuntimeError:
    HAS_DRMAA = False

# talking to mercurial
import hgapi

# CGAT specific options - later to be removed
from Local import *
from ruffus import *

from multiprocessing.pool import ThreadPool

import logging as L
from CGAT import Experiment as E
from CGAT import IOTools as IOTools
from CGAT import Requirements as Requirements
from CGATPipelines import Local as Local

# import into namespace for backwards compatibility
from CGAT.IOTools import cloneFile as clone
from CGAT.IOTools import touchFile as touch
from CGAT.IOTools import snip as snip

# global options and arguments - set but currently not
# used as relevant sections are entered into the PARAMS
# dictionary. Could be deprecated and removed.
GLOBAL_OPTIONS, GLOBAL_ARGS = None, None

# global drmaa session
GLOBAL_SESSION = None

# sort out script paths

# root directory of CGAT Code collection
CGATSCRIPTS_ROOT_DIR = os.path.dirname(os.path.dirname(E.__file__))
# CGAT Code collection scripts
CGATSCRIPTS_SCRIPTS_DIR = os.path.join(CGATSCRIPTS_ROOT_DIR, "scripts")


# root directory of CGAT Pipelines
CGATPIPELINES_ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
# CGAT Pipeline scripts
CGATPIPELINES_SCRIPTS_DIR = os.path.join(CGATPIPELINES_ROOT_DIR,
                                         "scripts")
# Directory of CGAT pipelines
CGATPIPELINES_PIPELINE_DIR = os.path.join(CGATPIPELINES_ROOT_DIR,
                                          "CGATPipelines")
# CGAT Pipeline R scripts
CGATPIPELINES_R_DIR = os.path.join(CGATPIPELINES_ROOT_DIR, "R")

# if Pipeline.py is called from an installed version, scripts are
# located in the "bin" directory.
if not os.path.exists(CGATSCRIPTS_SCRIPTS_DIR):
    SCRIPTS_DIR = os.path.join(sys.exec_prefix, "bin")

if not os.path.exists(CGATPIPELINES_SCRIPTS_DIR):
    PIPELINE_SCRIPTS_DIR = os.path.join(sys.exec_prefix, "bin")

# Global variable for configuration file data
CONFIG = ConfigParser.ConfigParser()

# Global variable for parameter interpolation in
# commands
# patch - if --help or -h in command line arguments,
# use a default dict as PARAMS to avaid missing paramater
# failures
if "--help" in sys.argv or "-h" in sys.argv:
    PARAMS = collections.defaultdict(str)
else:
    PARAMS = {}

# A list of hard-coded parameters within the CGAT environment
# These can be overwritten by command line options and
# configuration files
HARDCODED_PARAMS = {
    'scriptsdir': CGATSCRIPTS_SCRIPTS_DIR,
    'toolsdir': CGATSCRIPTS_SCRIPTS_DIR,
    'pipeline_scriptsdir': CGATPIPELINES_SCRIPTS_DIR,
    'pipelinedir': CGATPIPELINES_PIPELINE_DIR,
    'pipeline_rdir': CGATPIPELINES_R_DIR,
    # script to perform map/reduce like computation.
    'cmd-farm': """python %(pipeline_scriptsdir)s/farm.py
                --method=drmaa
                --bashrc=%(pipeline_scriptsdir)s/bashrc.cgat
                --cluster-options=%(cluster_options)s
                --cluster-queue=%(cluster_queue)s
                --cluster-num-jobs=%(cluster_num_jobs)i
                --cluster-priority=%(cluster_priority)i
    """,
    # command to get tab-separated output from database
    'cmd-sql': """sqlite3 -header -csv -separator $'\\t' """,
    # database backend
    'database_backend': "sqlite",
    # database host
    'database_host': "",
    # name of database
    'database_name': "csvdb",
    # database connection options
    'database_username': "cgat",
    # database password - if required
    'database_password': "",
    # database port - if required
    'database_port': 3306,
    # wrapper around non-CGAT scripts
    'cmd-run': """%(pipeline_scriptsdir)s/run.py""",
    # directory used for temporary local files
    'tmpdir': os.environ.get("TMPDIR", '/scratch'),
    # directory used for temporary files shared across machines
    'shared_tmpdir': os.environ.get("SHARED_TMPDIR", "/ifs/scratch"),
    # cluster queue to use
    'cluster_queue': 'all.q',
    # priority of jobs in cluster queue
    'cluster_priority': -10,
    # number of jobs to submit to cluster queue
    'cluster_num_jobs': 100,
    # name of consumable resource to use for requesting memory
    'cluster_memory_resource': "mem_free",
    # amount of memory set by default for each job
    'cluster_memory_default': "2G",
    # general cluster options
    'cluster_options': "",
    # parallel environment to use for multi-threaded jobs
    'cluster_parallel_environment': 'dedicated',
    # ruffus job limits for databases
    'jobs_limit_db': 10,
    # ruffus job limits for R
    'jobs_limit_R': 1,
}

# After all configuration files have been read, some
# parameters need to be interpolated with other parameters
# The list is below:
INTERPOLATE_PARAMS = ('cmd-farm', 'cmd-run')

# drop PARAMS variable into the Local module until parameter
# sharing is resolved between the Local module
# and the Pipeline module.
Local.PARAMS = PARAMS
Local.CONFIG = CONFIG

# set working directory at process launch to prevent repeated calls to
# os.getcwd failing if network is busy
WORKING_DIRECTORY = os.getcwd()


class PipelineError(Exception):
    pass


def configToDictionary(config):
    """convert the contents of a :py:class:`ConfigParser.ConfigParser`
    object to a dictionary

    This method works by iterating over all configuration values in a
    :py:class:`ConfigParser.ConfigParser` object and inserting values
    into a dictionary. Section names are prefixed using and underscore.
    Thus::

        [sample]
        name=12

    is entered as ``sample_name=12`` into the dictionary. The sections
    ``general`` and ``DEFAULT`` are treated specially in that both
    the prefixed and the unprefixed values are inserted: ::

       [general]
       genome=hg19

    will be added as ``general_genome=hg19`` and ``genome=hg19``.

    Numbers will be automatically recognized as such and converted into
    integers or floats.

    Returns
    -------
    config : dict
        A dictionary of configuration values

    """
    p = {}
    for section in config.sections():
        for key, value in config.items(section):
            try:
                v = IOTools.str2val(value)
            except TypeError:
                E.error("error converting key %s, value %s" % (key, value))
                E.error("Possible multiple concurrent attempts to "
                        "read configuration")
                raise

            p["%s_%s" % (section, key)] = v
            if section in ("general", "DEFAULT"):
                p["%s" % (key)] = v

    for key, value in config.defaults().iteritems():
        p["%s" % (key)] = IOTools.str2val(value)

    return p


def getParameters(filenames=["pipeline.ini", ],
                  defaults=None,
                  user_ini=True,
                  default_ini=True,
                  only_import=None):
    '''read a config file and return as a dictionary.

    Sections and keys are combined with an underscore. If a key
    without section does not exist, it will be added plain.

    For example::

       [general]
       input=input1.file

       [special]
       input=input2.file

    will be entered as { 'general_input' : "input1.file",
    'input: "input1.file", 'special_input' : "input2.file" }

    This function also updates the module-wide parameter map.

    The section [DEFAULT] is equivalent to [general].

    The order of initialization is as follows:

    1. hard-coded defaults
    2. pipeline specific default file in the CGAT code installation
    3. :file:`.cgat` in the users home directory
    4. files supplied by the user in the order given

    If the same configuration value appears in multiple
    files, later configuration files will overwrite the
    settings form earlier files.

    Path names are expanded to the absolute pathname to avoid
    ambiguity with relative path names. Path names are updated
    for parameters that end in the suffix "dir" and start with
    a "." such as "." or "../data".

    Arguments
    ---------
    filenames : list
       List of filenames of the configuration files to read.
    defaults : dict
       Dictionary with default values. These will be overwrite
       any hard-coded parameters, but will be overwritten by user
       specified parameters in the configuration files.
    default_ini : bool
       If set, the default initialization file will be read from
       'CGATPipelines/configuration/pipeline.ini'
    user_ini : bool
       If set, configuration files will also be read from a
       file called :file:`.cgat` in the user`s home directory.
    only_import : bool
       If set to a boolean, the parameter dictionary will be a
       defaultcollection. This is useful for pipelines that are
       imported (for example for documentation generation) but not
       executed as there might not be an appropriate .ini file
       available. If `only_import` is None, it will be set to the
       default, which is to raise an exception unless the calling
       script is imported or the option ``--is-test`` has been passed
       at the command line.

    Returns
    -------
    config : dict
       Dictionary with configuration values.
    '''

    global CONFIG
    global PARAMS
    caller_locals = getCallerLocals()

    # check if this is only for import
    if only_import is None:
        only_import = isTest() or \
            "__name__" not in caller_locals or \
            caller_locals["__name__"] != "__main__"

    # important: only update the PARAMS variable as
    # it is referenced in other modules.
    if only_import:
        d = collections.defaultdict(str)
        d.update(PARAMS)
        PARAMS = d

    if user_ini:
        # read configuration from a users home directory
        fn = os.path.join(os.path.expanduser("~"),
                          ".cgat")
        if os.path.exists(fn):
            filenames.insert(0, fn)

    # IMS: Several legacy scripts call this with a sting as input
    # rather than a list. Check for this and correct

    if isinstance(filenames, basestring):
        filenames = [filenames]

    if default_ini:
        # The link between CGATPipelines and Pipeline.py
        # needs to severed at one point.
        # 1. config files into CGAT module directory?
        # 2. Pipeline.py into CGATPipelines module directory?
        filenames.insert(0,
                         os.path.join(CGATPIPELINES_PIPELINE_DIR,
                                      'configuration',
                                      'pipeline.ini'))

    CONFIG.read(filenames)

    p = configToDictionary(CONFIG)

    # update with hard-coded PARAMS
    PARAMS.update(HARDCODED_PARAMS)

    if defaults:
        PARAMS.update(defaults)
    PARAMS.update(p)

    # interpolate some params with other parameters
    for param in INTERPOLATE_PARAMS:
        try:
            PARAMS[param] = PARAMS[param] % PARAMS
        except TypeError(msg):
            raise TypeError('could not interpolate %s: %s' %
                            (PARAMS[param], msg))

    # expand pathnames
    for param, value in PARAMS.items():
        if param.endswith("dir"):
            if value.startswith("."):
                PARAMS[param] = os.path.abspath(value)

    return PARAMS


def loadParameters(filenames):
    '''load parameters from one or more files.

    Parameters are processed in the same way as :func:`getParameters`,
    but the global parameter dictionary is not updated.

    Arguments
    ---------
    filenames : list
       List of filenames of the configuration files to read.

    Returns
    -------
    config : dict
       A configuration dictionary.

    '''
    config = ConfigParser.ConfigParser()
    config.read(filenames)

    p = configToDictionary(config)
    return p


def matchParameter(param):
    '''find an exact match or prefix-match in the global
    configuration dictionary param.

    Arguments
    ---------
    param : string
        Parameter to search for.

    Returns
    -------
    name : string
        The full parameter name.

    Raises
    ------
    KeyError if param can't be matched.

    '''
    if param in PARAMS:
        return param

    for key in PARAMS.keys():
        if "%" in key:
            rx = re.compile(re.sub("%", ".*", key))
            if rx.search(param):
                return key

    raise KeyError("parameter '%s' can not be matched in dictionary" %
                   param)


def substituteParameters(**kwargs):
    '''return a parameter dictionary.

    This method builds a dictionary of parameter values to
    apply for a specific task. The dictionary is built in
    the following order:

    1. take values from the global dictionary (:py:data:`PARAMS`)
    2. substitute values appearing in `kwargs`.
    3. Apply task specific configuration values by looking for the
       presence of ``outfile`` in kwargs.

    The substition of task specific values works by looking for any
    parameter values starting with the value of ``outfile``.  The
    suffix of the parameter value will then be substituted.

    For example::

        PARAMS = {"tophat_threads": 4,
                  "tophat_cutoff": 0.5,
                  "sample1.bam.gz_tophat_threads" : 6}
        outfile = "sample1.bam.gz"
        print substituteParameters(**locals())
        {"tophat_cutoff": 0.5, "tophat_threads": 6}

    Returns
    -------
    params : dict
        Dictionary with parameter values.

    '''

    # build parameter dictionary
    # note the order of addition to make sure that kwargs takes precedence
    local_params = dict(PARAMS.items() + kwargs.items())

    if "outfile" in local_params:
        # replace specific parameters with task (outfile) specific parameters
        outfile = local_params["outfile"]
        for k in local_params.keys():
            if k.startswith(outfile):
                p = k[len(outfile) + 1:]
                if p not in local_params:
                    raise KeyError(
                        "task specific parameter '%s' "
                        "does not exist for '%s' " % (p, k))
                E.debug("substituting task specific parameter "
                        "for %s: %s = %s" %
                        (outfile, p, local_params[k]))
                local_params[p] = local_params[k]

    return local_params


def asList(value):
    '''return a value as a list.

    If the value is a string and contains a ``,``, the string will
    be split at ``,``.

    Returns
    -------
    list

    '''
    if type(value) == str:
        try:
            values = [x.strip() for x in value.strip().split(",")]
        except AttributeError:
            values = [value.strip()]
        return [x for x in values if x != ""]
    elif type(value) in (types.ListType, types.TupleType):
        return value
    else:
        return [value]


def isTrue(param, **kwargs):
    '''return True if param has a True value.

    A parameter is False if it is:

    * not set
    * 0
    * the empty string
    * false or False

    Otherwise the value is True.

    Arguments
    ---------
    param : string
        Parameter to be tested
    kwargs : dict
        Dictionary of local configuration values. These will be passed
        to :func:`substituteParameters` before evaluating `param`

    Returns
    -------
    bool

    '''
    if kwargs:
        p = substituteParameters(**kwargs)
    else:
        p = PARAMS
    value = p.get(param, 0)
    return value not in (0, '', 'false', 'False')


def getTempFile(dir=None, shared=False):
    '''get a temporary file.

    The file is created and the caller needs to close and delete
    the temporary file once it is not used any more.

    Arguments
    ---------
    dir : string
        Directory of the temporary file and if not given is set to the
        default temporary location in the global configuration dictionary.
    shared : bool
        If set, the tempory file will be in a shared temporary
        location (given by the global configuration directory).

    Returns
    -------
    file : File
        A file object of the temporary file.
    '''
    if dir is None:
        if shared:
            dir = PARAMS['shared_tmpdir']
        else:
            dir = PARAMS['tmpdir']

    return tempfile.NamedTemporaryFile(dir=dir, delete=False, prefix="ctmp")


def getTempFilename(dir=None, shared=False):
    '''return a temporary filename.

    The file is created and the caller needs to delete the temporary
    file once it is not used any more.

    Arguments
    ---------
    dir : string
        Directory of the temporary file and if not given is set to the
        default temporary location in the global configuration dictionary.
    shared : bool
        If set, the tempory file will be in a shared temporary
        location.

    Returns
    -------
    filename : string
        Absolute pathname of temporary file.

    '''
    tmpfile = getTempFile(dir=dir, shared=shared)
    tmpfile.close()
    return tmpfile.name


def getTempDir(dir=None, shared=False):
    '''get a temporary directory.

    The directory is created and the caller needs to delete the temporary
    directory once it is not used any more.

    Arguments
    ---------
    dir : string
        Directory of the temporary directory and if not given is set to the
        default temporary location in the global configuration dictionary.
    shared : bool
        If set, the tempory directory will be in a shared temporary
        location.

    Returns
    -------
    filename : string
        Absolute pathname of temporary file.

    '''
    if dir is None:
        if shared:
            dir = PARAMS['shared_tmpdir']
        else:
            dir = PARAMS['tmpdir']

    return tempfile.mkdtemp(dir=dir, prefix="ctmp")


def checkExecutables(filenames):
    """check for the presence/absence of executables"""

    missing = []

    for filename in filenames:
        if not IOTools.which(filename):
            missing.append(filename)

    if missing:
        raise ValueError("missing executables: %s" % ",".join(missing))


def checkScripts(filenames):
    """check for the presence/absence of scripts"""
    missing = []
    for filename in filenames:
        if not os.path.exists(filename):
            missing.append(filename)

    if missing:
        raise ValueError("missing scripts: %s" % ",".join(missing))


def checkParameter(param):
    """check if parameter ``key`` is set"""
    if param not in PARAMS:
        raise ValueError("need `%s` to be set" % param)


def isTest():
    '''return True if the pipeline is run in a "testing" mode
    (command line options --is-test has been given).'''

    # note: do not test GLOBAL_OPTIONS as this method might have
    # been called before main()
    return "--is-test" in sys.argv


def tablequote(track):
    '''quote a track name such that is suitable as a table name.'''
    return re.sub("[-(),\[\].]", "_", track)


def toTable(outfile):
    '''convert a filename from a load statement into a table name.

    This method checks if the filename ends with ".load". The suffix
    is then removed and the filename quoted so that it is suitable
    as a table name.

    Arguments
    ---------
    outfile : string
        A filename ending in ".load".

    Returns
    -------
    tablename : string

    '''
    assert outfile.endswith(".load")
    name = os.path.basename(outfile[:-len(".load")])
    return tablequote(name)


def build_load_statement(tablename, retry=True, options=""):
    """build a command line statement to upload data.

    Upload is performed via the :doc:`csv2db` script.

    The returned statement is suitable to use in pipe expression.
    This method is aware of the configuration values for database
    access and the chosen database backend.

    For example::

        load_statement = P.build_load_statement("data")
        statement = "cat data.txt | %(load_statement)s"
        P.run()

    Arguments
    ---------
    tablename : string
        Tablename for upload
    retry : bool
        Add the ``--retry`` option to `csv2db.py`
    options : string
        Command line options to be passed on to `csv2db.py`

    Returns
    -------
    string

    """

    opts = []

    if retry:
        opts.append(" --retry ")

    backend = PARAMS["database_backend"]

    if backend not in ("sqlite", "mysql", "postgres"):
        raise NotImplementedError(
            "backend %s not implemented" % backend)

    opts.append("--database-backend=%s" % backend)
    opts.append("--database-name=%s" %
                PARAMS.get("database_name"))
    opts.append("--database-host=%s" %
                PARAMS.get("database_host", ""))
    opts.append("--database-user=%s" %
                PARAMS.get("database_username", ""))
    opts.append("--database-password=%s" %
                PARAMS.get("database_password", ""))
    opts.append("--database-port=%s" %
                PARAMS.get("database_port", 3306))

    db_options = " ".join(opts)

    statement = ('''
    python %(scriptsdir)s/csv2db.py
    %(db_options)s
    %(options)s
    --table=%(tablename)s
    ''')

    load_statement = buildStatement(**locals())

    return load_statement


def load(infile,
         outfile=None,
         options="",
         collapse=False,
         transpose=False,
         tablename=None,
         retry=True,
         limit=0,
         shuffle=False,
         job_memory=None):
    """import data from a tab-separated file into database.

    The table name is given by outfile without the
    ".load" suffix.

    A typical load task in ruffus would look like this::

        @transform("*.tsv.gz", suffix(".tsv.gz"), ".load")
        def loadData(infile, outfile):
            P.load(infile, outfile)

    Upload is performed via the :doc:`csv2db` script.

    Arguments
    ---------
    infile : string
        Filename of the input data
    outfile : string
        Output filename. This will contain the logging information. The
        table name is derived from `outfile` if `tablename` is not set.
    options : string
        Command line options for the `csv2db.py` script.
    collapse : string
        If set, the table will be collapsed before loading. This
        transforms a data set with two columns where the first column
        is the row name into a multi-column table.  The value of
        collapse is the value used for missing values.
    transpose : string
        If set, the table will be transposed before loading. The first
        column in the first row will be set to the string within
        transpose.
    retry : bool
        If True, multiple attempts will be made if the data can
        not be loaded at the first try, for example if a table is locked.
    limit : int
        If set, only load the first n lines.
    shuffle : bool
        If set, randomize lines before loading. Together with `limit`
        this permits loading a sample of rows.
    job_memory : string
        Amount of memory to allocate for job. If unset, uses the global
        default.
    """

    if job_memory is None:
        job_memory = PARAMS["cluster_memory_default"]

    if not tablename:
        tablename = toTable(outfile)

    statement = []

    if infile.endswith(".gz"):
        statement.append("zcat %(infile)s")
    else:
        statement.append("cat %(infile)s")

    if collapse:
        statement.append(
            "python %(scriptsdir)s/table2table.py --collapse=%(collapse)s")

    if transpose:
        statement.append(
            """python %(scriptsdir)s/table2table.py --transpose
            --set-transpose-field=%(transpose)s""")

    if shuffle:
        statement.append("perl %(scriptsdir)s/randomize_lines.pl -h")

    if limit > 0:
        # use awk to filter in order to avoid a pipeline broken error from head
        statement.append("awk 'NR > %i {exit(0)} {print}'" % (limit + 1))
        # ignore errors from cat or zcat due to broken pipe
        ignore_pipe_errors = True

    statement.append(build_load_statement(tablename,
                                          options=options,
                                          retry=retry))

    statement = " | ".join(statement) + " > %(outfile)s"

    run()


def concatenateAndLoad(infiles,
                       outfile,
                       regex_filename=None,
                       header=None,
                       cat="track",
                       has_titles=True,
                       missing_value="na",
                       retry=True,
                       options="",
                       job_memory=None):
    """concatenate multiple tab-separated files and upload into database.

    The table name is given by outfile without the
    ".load" suffix.

    A typical concatenate and load task in ruffus would look like this::

        @merge("*.tsv.gz", ".load")
        def loadData(infile, outfile):
            P.concatenateAndLoad(infiles, outfile)

    Upload is performed via the :doc:`csv2db` script.

    Arguments
    ---------
    infiles : list
        Filenames of the input data
    outfile : string
        Output filename. This will contain the logging information. The
        table name is derived from `outfile`.
    regex_filename : string
        If given, *regex_filename* is applied to the filename to extract
        the track name. If the pattern contains multiple groups, they are
        added as additional columns. For example, if `cat` is set to
        ``track,method`` and `regex_filename` is ``(.*)_(.*).tsv.gz``
        it will add the columns ``track`` and method to the table.
    header : string
        Comma-separated list of values for header.
    cat : string
        Column title for column containing the track name. The track name
        is derived from the filename, see `regex_filename`.
    has_titles : bool
        If True, files are expected to have column titles in their first row.
    missing_value : string
        String to use for missing values.
    retry : bool
        If True, multiple attempts will be made if the data can
        not be loaded at the first try, for example if a table is locked.
    options : string
        Command line options for the `csv2db.py` script.
    job_memory : string
        Amount of memory to allocate for job. If unset, uses the global
        default.

    """
    if job_memory is None:
        job_memory = PARAMS["cluster_memory_default"]

    infiles = " ".join(infiles)

    passed_options = options
    load_options, cat_options = ["--add-index=track"], []

    if regex_filename:
        cat_options.append("--regex-filename='%s'" % regex_filename)

    if header:
        load_options.append("--header-names=%s" % header)

    if not has_titles:
        cat_options.append("--no-titles")

    cat_options = " ".join(cat_options)
    load_options = " ".join(load_options) + " " + passed_options

    load_statement = build_load_statement(toTable(outfile),
                                          options=load_options,
                                          retry=retry)

    statement = '''python %(scriptsdir)s/combine_tables.py
    --cat=%(cat)s
    --missing-value=%(missing_value)s
    %(cat_options)s
    %(infiles)s
    | %(load_statement)s
    > %(outfile)s'''

    run()


def mergeAndLoad(infiles,
                 outfile,
                 suffix=None,
                 columns=(0, 1),
                 regex=None,
                 row_wise=True,
                 retry=True,
                 options="",
                 prefixes=None):
    '''merge multiple categorical tables and load into a database.

    The tables are merged and entered row-wise, i.e, the contents of
    each file are a row.

    For example, the statement::

        mergeAndLoad(['file1.txt', 'file2.txt'],
                     "test_table.load")

    with the two files::
        > cat file1.txt
        Category    Result
        length      12
        width       100

        > cat file2.txt
        Category    Result
        length      20
        width       50

    will be added into table ``test_table`` as::
        track   length   width
        file1   12       100
        file2   20       50

    If row-wise is set::
        mergeAndLoad(['file1.txt', 'file2.txt'],
                     "test_table.load", row_wise=True)

    ``test_table`` will be transposed and look like this::
        track    file1 file2
        length   12    20
        width    20    50

    Arguments
    ---------
    infiles : list
        Filenames of the input data
    outfile : string
        Output filename. This will contain the logging information. The
        table name is derived from `outfile`.
    suffix : string
        If `suffix` is given, the suffix will be removed from the filenames.
    columns : list
        The columns to be taken. By default, the first two columns are
        taken with the first being the key. Filenames are stored in a
        ``track`` column. Directory names are chopped off.  If
        `columns` is set to None, all columns will be taken. Here,
        column names will receive a prefix given by `prefixes`. If
        `prefixes` is None, the filename will be added as a prefix.
    regex : string
        If set, the full filename will be used to extract a
        track name via the supplied regular expression.
    row_wise : bool
        If set to False, each table will be a column in the resulting
        table.  This is useful if histograms are being merged.
    retry : bool
        If True, multiple attempts will be made if the data can
        not be loaded at the first try, for example if a table is locked.
    options : string
        Command line options for the `csv2db.py` script.
    prefixes : list
        If given, the respective prefix will be added to each
        column. The number of `prefixes` and `infiles` needs to be the
        same.

    '''
    if len(infiles) == 0:
        raise ValueError("no files for merging")

    if suffix:
        header = ",".join([os.path.basename(snip(x, suffix)) for x in infiles])
    elif regex:
        header = ",".join(["-".join(re.search(regex, x).groups())
                          for x in infiles])
    else:
        header = ",".join([os.path.basename(x) for x in infiles])

    header_stmt = "--header-names=%s" % header

    if columns:
        column_filter = "| cut -f %s" % ",".join(map(str,
                                                 [x + 1 for x in columns]))
    else:
        column_filter = ""
        if prefixes:
            assert len(prefixes) == len(infiles)
            header_stmt = "--prefixes=%s" % ",".join(prefixes)
        else:
            header_stmt = "--add-file-prefix"

    if infiles[0].endswith(".gz"):
        filenames = " ".join(
            ["<( zcat %s %s )" % (x, column_filter) for x in infiles])
    else:
        filenames = " ".join(
            ["<( cat %s %s )" % (x, column_filter) for x in infiles])

    if row_wise:
        transform = """| perl -p -e "s/bin/track/"
        | python %(scriptsdir)s/table2table.py --transpose""" % PARAMS
    else:
        transform = ""

    load_statement = build_load_statement(
        toTable(outfile),
        options="--add-index=track " + options,
        retry=retry)

    statement = """python %(scriptsdir)s/combine_tables.py
    %(header_stmt)s
    --skip-titles
    --missing-value=0
    --ignore-empty
    %(filenames)s
    %(transform)s
    | %(load_statement)s
    > %(outfile)s
    """
    run()


def connect():
    """connect to SQLite database used in this pipeline.

    .. note::
       This method is currently only implemented for sqlite
       databases. It needs refactoring for generic access.
       Alternatively, use an full or partial ORM.

    If ``annotations_database`` is in PARAMS, this method
    will attach the named database as ``annotations``.

    Returns
    -------
    dbh 
       a database handle

    """

    # Note that in the future this might return an sqlalchemy or
    # db.py handle.

    if PARAMS["database_backend"] == "sqlite":
        dbh = sqlite3.connect(PARAMS["database"])

        if "annotations_database" in PARAMS:
            statement = '''ATTACH DATABASE '%s' as annotations''' % \
                        (PARAMS["annotations_database"])
            cc = dbh.cursor()
            cc.execute(statement)
            cc.close()
    else:
        raise NotImplementedError(
            "backend %s not implemented" % PARAMS["database_backend"])
    return dbh


def createView(dbhandle, tables, tablename, outfile,
               view_type="TABLE",
               ignore_duplicates=True):
    '''create a database view for a list of tables.

    This method performs a join across multiple tables and stores the
    result either as a view or a table in the database.

    Arguments
    ---------
    dbhandle :
        A database handle.
    tables : list of tuples
        Tables to merge. Each tuple contains the name of a table and
        the field to join with the first table. For example::
            tables = (
                "reads_summary", "track",
                "bam_stats", "track",
                "context_stats", "track",
                "picard_stats_alignment_summary_metrics", "track")
    tablename : string
        Name of the view or table to be created.
    outfile : string
        Output filename for status information.
    view_type : string
        Type of view, either ``VIEW`` or ``TABLE``.  If a view is to be
        created across multiple databases, use ``TABLE``.
    ignore_duplicates : bool
        If set to False, duplicate column names will be added with the
        tablename as prefix. The default is to ignore.

    '''

    Database.executewait(
        dbhandle,
        "DROP %(view_type)s IF EXISTS %(tablename)s" % locals())

    tracks, columns = [], []
    tablenames = [x[0] for x in tables]
    for table, track in tables:
        d = Database.executewait(
            dbhandle,
            "SELECT COUNT(DISTINCT %s) FROM %s" % (track, table))
        tracks.append(d.fetchone()[0])
        columns.append(
            [x.lower() for x in Database.getColumnNames(dbhandle, table)
             if x != track])

    E.info("creating %s from the following tables: %s" %
           (tablename, str(zip(tablenames, tracks))))
    if min(tracks) != max(tracks):
        raise ValueError(
            "number of rows not identical - will not create view")

    from_statement = " , ".join(
        ["%s as t%i" % (y[0], x) for x, y in enumerate(tables)])
    f = tables[0][1]
    where_statement = " AND ".join(
        ["t0.%s = t%i.%s" % (f, x + 1, y[1])
         for x, y in enumerate(tables[1:])])

    all_columns, taken = [], set()
    for x, c in enumerate(columns):
        i = set(taken).intersection(set(c))
        if i:
            E.warn("duplicate column names: %s " % i)
            if not ignore_duplicates:
                table = tables[x][0]
                all_columns.extend(
                    ["t%i.%s AS %s_%s" % (x, y, table, y) for y in i])
                c = [y for y in c if y not in i]

        all_columns.extend(["t%i.%s" % (x, y) for y in c])
        taken.update(set(c))

    all_columns = ",".join(all_columns)
    statement = '''
    CREATE %(view_type)s %(tablename)s AS SELECT t0.track, %(all_columns)s
    FROM %(from_statement)s
    WHERE %(where_statement)s
    ''' % locals()

    Database.executewait(dbhandle, statement)

    nrows = Database.executewait(
        dbhandle, "SELECT COUNT(*) FROM view_mapping").fetchone()[0]

    if nrows == 0:
        raise ValueError(
            "empty view mapping, check statement = %s" %
            (statement % locals()))
    if nrows != min(tracks):
        E.warn("view creates duplicate rows, got %i, expected %i" %
               (nrows, min(tracks)))

    E.info("created view_mapping with %i rows" % nrows)
    touch(outfile)


def shellquote(statement):
    '''shell quote a string to be used as a function argument.

    from http://stackoverflow.com/questions/967443/python-module-to-shellquote-unshellquote
    '''
    _quote_pos = re.compile('(?=[^-0-9a-zA-Z_./\n])')

    if statement:
        return _quote_pos.sub('\\\\', statement).replace('\n', "'\n'")
    else:
        return "''"


def getCallerLocals(decorators=0):
    '''returns the locals of the calling function.

    from http://pylab.blogspot.com/2009/02/python-accessing-caller-locals-from.html

    Arguments
    ---------
    decorators : int
        Number of contexts to go up to reach calling function
        of interest.

    Returns
    -------
    locals : dict
        Dictionary of variable defined in the context of the
        calling function.
    '''
    f = sys._getframe(2 + decorators)
    args = inspect.getargvalues(f)
    return args[3]


def getCaller(decorators=0):
    """return the name of the calling module.

    Arguments
    ---------
    decorators : int
        Number of contexts to go up to reach calling function
        of interest.

    Returns
    -------
    mod : object
        The calling module
    """
    
    frm = inspect.stack()[2 + decorators]
    mod = inspect.getmodule(frm[0])
    return mod


def execute(statement, **kwargs):
    '''execute a statement locally.

    This method implements the same parameter interpolation
    as the function :func:`run`.

    Arguments
    ---------
    statement : string
        Command line statement to run.

    Returns
    -------
    stdout : string
        Data sent to standard output by command
    stderr : string
        Data sent to standard error by command
    '''

    if not kwargs:
        kwargs = getCallerLocals()

    kwargs = dict(PARAMS.items() + kwargs.items())

    L.debug("running %s" % (statement % kwargs))

    if "cwd" not in kwargs:
        cwd = WORKING_DIRECTORY
    else:
        cwd = kwargs["cwd"]

    # cleaning up of statement
    # remove new lines and superfluous spaces and tabs
    statement = " ".join(re.sub("\t+", " ", statement).split("\n")).strip()
    if statement.endswith(";"):
        statement = statement[:-1]

    process = subprocess.Popen(statement % kwargs,
                               cwd=cwd,
                               shell=True,
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)

    # process.stdin.close()
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        raise PipelineError(
            "Child was terminated by signal %i: \n"
            "The stderr was: \n%s\n%s\n" %
            (-process.returncode, stderr, statement))

    return stdout, stderr

# Definition of helper functions for job scripts
# detect_pipe_error(): propagate error of programs not at the end of a pipe
# checkpoint(): exit a set of chained commands (via ;) if the previous
# command failed.
_exec_prefix = '''detect_pipe_error_helper()
    {
    while [ "$#" != 0 ] ; do
        # there was an error in at least one program of the pipe
        if [ "$1" != 0 ] ; then return 1 ; fi
        shift 1
    done
    return 0
    }
    detect_pipe_error() {
    detect_pipe_error_helper "${PIPESTATUS[@]}"
    return $?
    }
    checkpoint() {
        detect_pipe_error;
        if [ $? != 0 ]; then exit 1; fi;
    }
    '''

_exec_suffix = "; detect_pipe_error"


def buildStatement(**kwargs):
    '''build a command line statement with paramater interpolation.

    The skeleton of the statement should be defined in kwargs.  The
    method then applies string interpolation using a dictionary built
    from the global configuration dictionary PARAMS, but augmented by
    `kwargs`. The latter takes precedence.
    
    Arguments
    ---------
    kwargs : dict
        Keyword arguments that are used for parameter interpolation.

    Returns
    -------
    statement : string
        The command line statement with interpolated parameters.

    Raises
    ------
    ValueError
        If ``statement`` is not a key in `kwargs`.

    '''

    if "statement" not in kwargs:
        raise ValueError("'statement' not defined")

    local_params = substituteParameters(**kwargs)

    # build the statement
    try:
        statement = kwargs.get("statement") % local_params
    except KeyError, msg:
        raise KeyError(
            "Error when creating command: could not "
            "find %s in dictionaries" % msg)
    except ValueError, msg:
        raise ValueError("Error when creating command: %s, statement = %s" % (
            msg, kwargs.get("statement")))

    # cleaning up of statement
    # remove new lines and superfluous spaces and tabs
    statement = " ".join(re.sub("\t+", " ", statement).split("\n")).strip()
    if statement.endswith(";"):
        statement = statement[:-1]

    return statement


def expandStatement(statement, ignore_pipe_errors=False):
    '''add generic commands before and after statement.

    The prefixes and suffixes added are defined in 
    :data:`exec_prefix` and :data:`exec_suffix`. The
    main purpose of these prefixs is to provide error
    detection code to detect errors at early steps in
    a series of unix commands within a pipe.

    Arguments
    ---------
    statement : string
        Command line statement to expand
    ignore_pipe_errors : bool
        If False, do not modify statement.

    Returns
    -------
    statement : string
        The expanded statement.
    '''

    if ignore_pipe_errors:
        return statement
    else:
        return " ".join((_exec_prefix, statement, _exec_suffix))


def joinStatements(statements, infile):
    '''join a chain of statements into a single statement.

    Each statement contains an @IN@ or a @OUT@ placeholder or both.
    These will be replaced by the names of successive temporary files.

    In the first statement, @IN@ is replaced with `infile`.

    The last statement should move @IN@ to outfile.

    Arguments
    ---------
    statements : list
        A list of command line statements.
    infile : string
        Filename of the first data set.

    Returns
    -------
    statement : string
        A single command line statement.

    '''

    prefix = getTempFilename()
    pattern = "%s_%%i" % prefix

    result = []
    for x, statement in enumerate(statements):
        if x == 0:
            s = re.sub("@IN@", infile, statement)
        else:
            s = re.sub("@IN@", pattern % x, statement)

        s = re.sub("@OUT@", pattern % (x + 1), s).strip()

        if s.endswith(";"):
            s = s[:-1]
        result.append(s)

    assert prefix != ""
    result.append("rm -f %s*" % prefix)

    result = "; checkpoint ; ".join(result)
    return result


def getStdoutStderr(stdout_path, stderr_path, tries=5):
    '''get stdout/stderr allowing for same lag.

    Try at most *tries* times. If unsuccessfull, throw PipelineError.

    Removes the files once they are read.

    Returns tuple of stdout and stderr.
    '''
    x = tries
    while x >= 0:
        if os.path.exists(stdout_path):
            break
        time.sleep(1)
        x -= 1

    x = tries
    while x >= 0:
        if os.path.exists(stderr_path):
            break
        time.sleep(1)
        x -= 1

    try:
        stdout = open(stdout_path, "r").readlines()
    except IOError, msg:
        E.warn("could not open stdout: %s" % msg)
        stdout = []

    try:
        stderr = open(stderr_path, "r").readlines()
    except IOError, msg:
        E.warn("could not open stdout: %s" % msg)
        stderr = []

    try:
        os.unlink(stdout_path)
        os.unlink(stderr_path)
    except OSError, msg:
        pass

    return stdout, stderr


def _collectSingleJobFromCluster(session, job_id,
                                 statement,
                                 stdout_path, stderr_path,
                                 job_path,
                                 ignore_errors=False):
    '''runs a single job on the cluster.'''
    try:
        retval = session.wait(
            job_id, drmaa.Session.TIMEOUT_WAIT_FOREVER)
    except Exception, msg:
        # ignore message 24 in PBS code 24: drmaa: Job
        # finished but resource usage information and/or
        # termination status could not be provided.":
        if not msg.message.startswith("code 24"):
            raise
        retval = None

    stdout, stderr = getStdoutStderr(stdout_path, stderr_path)

    if retval and retval.exitStatus != 0 and not ignore_errors:
        raise PipelineError(
            "---------------------------------------\n"
            "Child was terminated by signal %i: \n"
            "The stderr was: \n%s\n%s\n"
            "-----------------------------------------" %
            (retval.exitStatus,
             "".join(stderr), statement))

    try:
        os.unlink(job_path)
    except OSError:
        L.warn(
            ("temporary job file %s not present for "
             "clean-up - ignored") % job_path)


def run(**kwargs):
    """run a command line statement.

    The method runs a single or multiple statements on the cluster
    using drmaa. The cluster is bypassed if:

        * ``to_cluster`` is set to None in the context of the
          calling function.

        * ``--local`` has been specified on the command line
          and the option ``without_cluster`` has been set as
          a result.

        * no libdrmaa is present

        * the global session is not initialized (GLOBAL_SESSION is
          None)

    To decide which statement to run, the method works by examining
    the context of the calling function for a variable called
    ``statement`` or ``statements``.

    If ``statements`` is defined, multiple job scripts are created and
    sent to the cluster. If ``statement`` is defined, a single job
    script is created and sent to the cluster. Additionally, if
    ``job_array`` is defined, the single statement will be submitted
    as an array job.

    Troubleshooting:

       1. DRMAA creates sessions and their is a limited number
          of sessions available. If there are two many or sessions
          become not available after failed jobs, use ``qconf -secl``
          to list sessions and ``qconf -kec #`` to delete sessions.

       2. Memory: 1G of free memory can be requested using the job_memory
          variable: ``job_memory = "1G"``
          If there are error messages like "no available queue", then the
          problem could be that a particular complex attribute has
          not been defined (the code should be ``hc`` for ``host:complex``
          and not ``hl`` for ``host:local``. Note that qrsh/qsub directly
          still works.

    """

    # combine options using correct preference
    options = dict(PARAMS.items())
    options.update(getCallerLocals().items())
    options.update(kwargs.items())

    # insert a few legacy synonyms
    options['cluster_options'] = options.get('job_options',
                                             options['cluster_options'])
    options['cluster_queue'] = options.get('job_queue',
                                           options['cluster_queue'])
    options['without_cluster'] = options.get('without_cluster')

    job_memory = None

    if 'job_memory' in options:
        job_memory = options['job_memory']

    elif "mem_free" in options["cluster_options"] and \
         PARAMS.get("cluster_memory_resource", False):

        E.warn("use of mem_free in job options is deprecated, please"
               " set job_memory local var instead")

        o = options["cluster_options"]
        x = re.search("-l\s*mem_free\s*=\s*(\S+)", o)
        if x is None:
            raise ValueError(
                "expecting mem_free in '%s'" % o)

        job_memory = x.groups()[0]

        # remove memory spec from job options
        options["cluster_options"] = re.sub(
            "-l\S*mem_free\s*=\s(\S+)", "", o)
    else:
        job_memory = PARAMS.get("cluster_memory_default", "2G")

    def setupJob(session, options, job_memory, job_name):

        jt = session.createJobTemplate()
        jt.workingDirectory = WORKING_DIRECTORY
        jt.jobEnvironment = {'BASH_ENV': '~/.bashrc'}
        jt.args = []
        if not re.match("[a-zA-Z]", job_name[0]):
            job_name = "_" + job_name

        spec = [
            "-V",
            "-q %(cluster_queue)s",
            "-p %(cluster_priority)i",
            "-N %s" % job_name,
            "%(cluster_options)s"]

        # limit memory of cluster jobs
        spec.append("-l %s=%s" % (PARAMS["cluster_memory_resource"],
                                  job_memory))

        # if process has multiple threads, use a parallel environment
        if 'job_threads' in options:
            spec.append(
                "-pe %(cluster_parallel_environment)s %(job_threads)i -R y")

        jt.nativeSpecification = " ".join(spec) % options
        # keep stdout and stderr separate
        jt.joinFiles = False

        return jt

    shellfile = os.path.join(WORKING_DIRECTORY, "shell.log")

    pid = os.getpid()
    L.debug('task: pid = %i' % pid)

    # connect to global session
    session = GLOBAL_SESSION
    L.debug('task: pid %i: sge session = %s' % (pid, str(session)))

    ignore_pipe_errors = options.get('ignore_pipe_errors', False)
    ignore_errors = options.get('ignore_errors', False)

    # run on cluster if:
    # * to_cluster is not defined or set to True
    # * command line option without_cluster is set to False
    # * an SGE session is present
    run_on_cluster = ("to_cluster" not in options or
                      options.get("to_cluster")) and \
        not options["without_cluster"] and \
        GLOBAL_SESSION is not None

    # SGE compatible job_name
    job_name = re.sub(
        "[:]", "_",
        os.path.basename(options.get("outfile", "ruffus")))

    def buildJobScript(statement, job_memory, job_name):
        '''build job script from statement.

        returns (name_of_script, stdout_path, stderr_path)
        '''

        tmpfile = getTempFile(dir=WORKING_DIRECTORY)
        # disabled: -l -O expand_aliases\n" )
        tmpfile.write("#!/bin/bash\n")
        tmpfile.write(
            'echo "%s : START -> %s" >> %s\n' %
            (job_name, tmpfile.name, shellfile))
        # disabled - problems with quoting
        # tmpfile.write( '''echo 'statement=%s' >> %s\n''' %
        # (shellquote(statement), shellfile) )
        tmpfile.write("set | sed 's/^/%s : /' &>> %s\n" %
                      (job_name, shellfile))
        # module list outputs to stderr, so merge stderr and stdout
        tmpfile.write("module list 2>&1 | sed 's/^/%s: /' &>> %s\n" %
                      (job_name, shellfile))
        tmpfile.write("hostname | sed 's/^/%s: /' &>> %s\n" %
                      (job_name, shellfile))
        tmpfile.write("cat /proc/meminfo | sed 's/^/%s: /' &>> %s\n" %
                      (job_name, shellfile))
        tmpfile.write(
            'echo "%s : END -> %s" >> %s\n' %
            (job_name, tmpfile.name, shellfile))

        # restrict virtual memory
        # Note that there are resources in SGE which could do this directly
        # such as v_hmem.
        # Note that limiting resident set sizes (RSS) with ulimit is not
        # possible in newer kernels.
        tmpfile.write("ulimit -v %i\n" % IOTools.human2bytes(job_memory))

        tmpfile.write(
            expandStatement(
                statement,
                ignore_pipe_errors=ignore_pipe_errors) + "\n")
        tmpfile.close()

        job_path = os.path.abspath(tmpfile.name)
        stdout_path = job_path + ".stdout"
        stderr_path = job_path + ".stderr"

        os.chmod(job_path, stat.S_IRWXG | stat.S_IRWXU)

        return (job_path, stdout_path, stderr_path)

    if run_on_cluster:
        # run multiple jobs
        if options.get("statements"):

            statement_list = []
            for statement in options.get("statements"):
                options["statement"] = statement
                statement_list.append(buildStatement(**options))

            if options.get("dryrun", False):
                return

            jt = setupJob(session, options, job_memory, job_name)

            job_ids, filenames = [], []
            for statement in statement_list:
                L.debug("running statement:\n%s" % statement)

                job_path, stdout_path, stderr_path = buildJobScript(statement,
                                                                    job_memory,
                                                                    job_name)

                jt.remoteCommand = job_path
                jt.outputPath = ":" + stdout_path
                jt.errorPath = ":" + stderr_path

                os.chmod(job_path, stat.S_IRWXG | stat.S_IRWXU)

                job_id = session.runJob(jt)
                job_ids.append(job_id)
                filenames.append((job_path, stdout_path, stderr_path))

                L.debug("job has been submitted with job_id %s" % str(job_id))

            L.debug("waiting for %i jobs to finish " % len(job_ids))
            session.synchronize(job_ids, drmaa.Session.TIMEOUT_WAIT_FOREVER,
                                False)

            # collect and clean up
            for job_id, statement, paths in zip(job_ids, statement_list,
                                                filenames):
                job_path, stdout_path, stderr_path = paths
                _collectSingleJobFromCluster(session, job_id,
                                             statement,
                                             stdout_path,
                                             stderr_path,
                                             job_path,
                                             ignore_errors=ignore_errors)

            session.deleteJobTemplate(jt)

        # run single job on cluster - this can be an array job
        else:

            statement = buildStatement(**options)
            L.debug("running statement:\n%s" % statement)

            if options.get("dryrun", False):
                return

            job_path, stdout_path, stderr_path = buildJobScript(statement,
                                                                job_memory,
                                                                job_name)

            jt = setupJob(session, options, job_memory, job_name)

            jt.remoteCommand = job_path
            # later: allow redirection of stdout and stderr to files;
            # can even be across hosts?
            jt.outputPath = ":" + stdout_path
            jt.errorPath = ":" + stderr_path

            if "job_array" in options and options["job_array"] is not None:
                # run an array job
                start, end, increment = options.get("job_array")
                L.debug("starting an array job: %i-%i,%i" %
                        (start, end, increment))
                # sge works with 1-based, closed intervals
                job_ids = session.runBulkJobs(jt, start + 1, end, increment)
                L.debug("%i array jobs have been submitted as job_id %s" %
                        (len(job_ids), job_ids[0]))
                retval = session.synchronize(
                    job_ids, drmaa.Session.TIMEOUT_WAIT_FOREVER, True)

                stdout, stderr = getStdoutStderr(stdout_path, stderr_path)

            else:
                # run a single job
                job_id = session.runJob(jt)
                L.debug("job has been submitted with job_id %s" % str(job_id))

                _collectSingleJobFromCluster(session, job_id,
                                             statement,
                                             stdout_path,
                                             stderr_path,
                                             job_path,
                                             ignore_errors=ignore_errors)

            session.deleteJobTemplate(jt)
    else:
        # run job locally on cluster
        statement_list = []
        if options.get("statements"):
            for statement in options.get("statements"):
                options["statement"] = statement
                statement_list.append(buildStatement(**options))
        else:
            statement_list.append(buildStatement(**options))

        if options.get("dryrun", False):
            return

        for statement in statement_list:
            L.debug("running statement:\n%s" % statement)

            # process substitution <() and >() does not
            # work through subprocess directly. Thus,
            # the statement needs to be wrapped in
            # /bin/bash -c '...' in order for bash
            # to interpret the substitution correctly.
            if "<(" in statement:
                shell = os.environ.get('SHELL', "/bin/bash")
                if "bash" not in shell:
                    raise ValueError(
                        "require bash for advanced shell syntax: <()")
                # Note: pipes.quote is deprecated in Py3, use shlex.quote
                # (not present in Py2.7).
                statement = pipes.quote(statement)
                statement = "%s -c %s" % (shell, statement)

            process = subprocess.Popen(
                expandStatement(
                    statement,
                    ignore_pipe_errors=ignore_pipe_errors),
                cwd=WORKING_DIRECTORY,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)

            # process.stdin.close()
            stdout, stderr = process.communicate()

            if process.returncode != 0 and not ignore_errors:
                raise PipelineError(
                    "---------------------------------------\n"
                    "Child was terminated by signal %i: \n"
                    "The stderr was: \n%s\n%s\n"
                    "-----------------------------------------" %
                    (-process.returncode, stderr, statement))


def submit(module, function, params=None,
           infiles=None, outfiles=None,
           to_cluster=True,
           logfile=None,
           job_options="",
           job_threads=1,
           job_memory=False):
    '''submit a python *function* as a job to the cluster.

    This method runs the script :file:`run_function` using the
    :func:`run` method in this module thus providing the same
    control options as for command line tools.

    Arguments
    ---------
    module : string
        Module name that contains the function. If `module` is
        not part of the PYTHONPATH, an absolute path can be given.
    function : string
        Name of function to execute
    infiles : string or list
        Filenames of input data
    outfiles : string or list
        Filenames of output data
    logfile : filename
        Logfile to provide to the ``--log`` option
    job_options : string
        String for generic job options for the queuing system
    job_threads : int
        Number of slots (threads/cores/CPU) to use for the task
    job_memory : string
        Amount of memory to reserve for the job.

    '''

    if not job_memory:
        job_memory = PARAMS.get("cluster_memory_default", "2G")

    if type(infiles) in (list, tuple):
        infiles = " ".join(["--input=%s" % x for x in infiles])
    else:
        infiles = "--input=%s" % infiles

    if type(outfiles) in (list, tuple):
        outfiles = " ".join(["--output-section=%s" % x for x in outfiles])
    else:
        outfiles = "--output-section=%s" % outfiles

    if logfile:
        logfile = "--log=%s" % logfile
    else:
        logfile = ""

    if params:
        params = "--params=%s" % ",".join(params)
    else:
        params = ""

    statement = '''python %(pipeline_scriptsdir)s/run_function.py
                          --module=%(module)s
                          --function=%(function)s
                          %(logfile)s
                          %(infiles)s
                          %(outfiles)s
                          %(params)s
                '''
    run()


def cluster_runnable(func):
    '''A dectorator that allows a function to be run on the cluster.

    The decorated function now takes extra arguments. The most important
    is *submit*. If set to true, it will submit the function to the cluster
    via the Pipeline.submit framework. Arguments to the function are
    pickled, so this will only work if arguments are picklable. Other
    arguments to submit are also accepted.

    Note that this allows the unusal combination of *submit* false,
    and *to_cluster* true. This will submit the function as an external
    job, but run it on the local machine.

    Note: all arguments in the decorated function must be passed as
    key-word arguments.
    '''

    # MM: when decorating functions with cluster_runnable, provide
    # them as kwargs, else will throw attribute error

    function_name = func.__name__

    def submit_function(*args, **kwargs):

        if "submit" in kwargs and kwargs["submit"]:
            del kwargs["submit"]
            submit_args, args_file = _pickle_args(args, kwargs)
            module_file = os.path.abspath(
                sys.modules[func.__module__].__file__)
            submit(snip(__file__),
                   "run_pickled",
                   params=[snip(module_file), function_name, args_file],
                   **submit_args)
        else:
            # remove job contral options before running function
            for x in ("submit", "job_options", "job_queue"):
                if x in kwargs:
                    del kwargs[x]
            return func(*args, **kwargs)

    return submit_function


def run_pickled(params):
    ''' run a function whose arguments have been pickled.

    expects that params is [module_name, function_name, arguments_file] '''

    module_name, func_name, args_file = params
    location = os.path.dirname(module_name)
    if location != "":
        sys.path.append(location)

    module_base_name = os.path.basename(module_name)
    E.info("importing module '%s' " % module_base_name)
    E.debug("sys.path is: %s" % sys.path)

    module = importlib.import_module(module_base_name)
    try:
        function = getattr(module, func_name)
    except AttributeError as msg:
        raise AttributeError(msg.message +
                             "unknown function, available functions are: %s" %
                             ",".join([x for x in dir(module)
                                       if not x.startswith("_")]))

    args, kwargs = pickle.load(open(args_file, "rb"))
    E.info("arguments = %s" % str(args))
    E.info("keyword arguments = %s" % str(kwargs))

    function(*args, **kwargs)

    os.unlink(args_file)


def run_report(clean=True,
               with_pipeline_status=True,
               pipeline_status_format="svg"):
    '''run CGATreport.

    This will also run ruffus to create an svg image of the pipeline
    status unless *with_pipeline_status* is set to False. The image
    will be saved into the export directory.

    '''

    if with_pipeline_status:
        targetdir = PARAMS["exportdir"]
        if not os.path.exists(targetdir):
            os.mkdir(targetdir)

        # get checksum level from command line options
        checksum_level = GLOBAL_OPTIONS.checksums

        pipeline_printout_graph(
            os.path.join(
                targetdir,
                "pipeline.%s" % pipeline_status_format),
            pipeline_status_format,
            ["full"],
            checksum_level=checksum_level
        )

    dirname, basename = os.path.split(getCaller().__file__)

    report_engine = PARAMS.get("report_engine", "cgatreport")
    assert report_engine in ('sphinxreport', 'cgatreport')

    docdir = os.path.join(dirname, "pipeline_docs", snip(basename, ".py"))
    themedir = os.path.join(dirname, "pipeline_docs", "themes")
    relpath = os.path.relpath(docdir)
    trackerdir = os.path.join(docdir, "trackers")
    job_memory = "4G"
    job_threads = PARAMS["report_threads"]

    # use a fake X display in order to avoid windows popping up
    # from R plots.
    xvfb_command = IOTools.which("xvfb-run")

    # permit multiple servers using -a option
    if xvfb_command:
        xvfb_command += " -a "
    else:
        xvfb_command = ""

    # if there is no DISPLAY variable set, xvfb runs, but
    # exits with error when killing process. Thus, ignore return
    # value.
    # print os.getenv("DISPLAY"), "command=", xvfb_command
    if not os.getenv("DISPLAY"):
        erase_return = "|| true"
    else:
        erase_return = ""

    # in the current version, xvfb always returns with an error, thus
    # ignore these.
    erase_return = "|| true"

    if clean:
        clean = """rm -rf report _cache _static;"""
    else:
        clean = ""

    # with sphinx >1.3.1 the PYTHONPATH needs to be set explicitely as
    # the virtual environment seems to be stripped. It is thus set to
    # the contents of the current sys.path
    syspath = ":".join(sys.path)

    statement = '''
    %(clean)s
    (export SPHINX_DOCSDIR=%(docdir)s;
    export SPHINX_THEMEDIR=%(themedir)s;
    export PYTHONPATH=%(syspath)s;
    %(xvfb_command)s
    %(report_engine)s-build
           --num-jobs=%(report_threads)s
           sphinx-build
                    -b html
                    -d %(report_doctrees)s
                    -c .
           %(docdir)s %(report_html)s
    >& report.log %(erase_return)s )
    '''

    run()

    L.info('the report is available at %s' % os.path.abspath(
        os.path.join(PARAMS['report_html'], "contents.html")))


def publish_notebooks():
    '''publish report into web directory.'''

    dirs = getProjectDirectories()

    notebookdir = dirs['notebookdir']
    exportdir = dirs['exportdir']
    exportnotebookdir = os.path.join(exportdir, "notebooks")

    if not os.path.exists(exportnotebookdir):
        os.makedirs(exportnotebookdir)

    statement = '''
    cd %(exportnotebookdir)s;
    ipython nbconvert
    %(notebookdir)s/*.ipynb
    --to html
    ''' % locals()

    E.run(statement)


def clonePipeline(srcdir, destdir=None):
    '''clone a pipeline.

    Cloning entails creating a mirror of the source pipeline.
    Generally, data files are mirrored by linking. Configuration
    files and the pipeline database will be copied.

    Without modification of any files, building the cloned pipeline in
    `destdir` should not re-run any commands. However, on deleting
    selected files, the pipeline should run from the appropriate
    point.  Newly created files will not affect the original pipeline.
    
    Cloning pipelines permits sharing partial results between
    pipelines, for example for parameter optimization.
    
    Arguments
    ---------
    scrdir : string
        Source directory
    destdir : string
        Destination directory. If None, use the current directory.

    '''

    if destdir is None:
        destdir = os.path.curdir

    E.info("cloning pipeline from %s to %s" % (srcdir, destdir))

    copy_files = ("conf.py", "pipeline.ini", "csvdb")
    ignore_prefix = (
        "report", "_cache", "export", "tmp", "ctmp",
        "_static", "_templates")

    def _ignore(p):
        for x in ignore_prefix:
            if p.startswith(x):
                return True
        return False

    for root, dirs, files in os.walk(srcdir):

        relpath = os.path.relpath(root, srcdir)
        if _ignore(relpath):
            continue

        for d in dirs:
            if _ignore(d):
                continue
            dest = os.path.join(os.path.join(destdir, relpath, d))
            os.mkdir(dest)
            # touch
            s = os.stat(os.path.join(root, d))
            os.utime(dest, (s.st_atime, s.st_mtime))

        for f in files:
            if _ignore(f):
                continue

            fn = os.path.join(root, f)
            dest_fn = os.path.join(destdir, relpath, f)
            if f in copy_files:
                shutil.copyfile(fn, dest_fn)
            else:
                # realpath resolves links - thus links will be linked to
                # the original target
                os.symlink(os.path.realpath(fn),
                           dest_fn)


def writeConfigFiles(path):
    '''create default configuration files in `path`.
    '''

    for dest in ("pipeline.ini", "conf.py"):
        src = os.path.join(path, dest)
        if os.path.exists(dest):
            L.warn("file `%s` already exists - skipped" % dest)
            continue

        if not os.path.exists(src):
            raise ValueError("default config file `%s` not found" % src)

        shutil.copyfile(src, dest)
        L.info("created new configuration file `%s` " % dest)


def clean(files, logfile):
    '''clean up files given by glob expressions.

    Files are cleaned up by zapping, i.e. the files are set to size
    0. Links to files are replaced with place-holders.

    Information about the original file is written to `logfile`.

    Arguments
    ---------
    files : list
        List of glob expressions of files to clean up.
    logfile : string
        Filename of logfile.

    '''
    fields = ('st_atime', 'st_blksize', 'st_blocks',
              'st_ctime', 'st_dev', 'st_gid', 'st_ino',
              'st_mode', 'st_mtime', 'st_nlink',
              'st_rdev', 'st_size', 'st_uid')

    dry_run = PARAMS.get("dryrun", False)

    if not dry_run:
        if not os.path.exists(logfile):
            outfile = IOTools.openFile(logfile, "w")
            outfile.write("filename\tzapped\tlinkdest\t%s\n" %
                          "\t".join(fields))
        else:
            outfile = IOTools.openFile(logfile, "a")

    c = E.Counter()
    for fn in files:
        c.files += 1
        if not dry_run:
            stat, linkdest = IOTools.zapFile(fn)
            if stat is not None:
                c.zapped += 1
                if linkdest is not None:
                    c.links += 1
                outfile.write("%s\t%s\t%s\t%s\n" % (
                    fn,
                    time.asctime(time.localtime(time.time())),
                    linkdest,
                    "\t".join([str(getattr(stat, x)) for x in fields])))

    L.info("zapped: %s" % (c))
    outfile.close()

    return c


def peekParameters(workingdir,
                   pipeline,
                   on_error_raise=None,
                   prefix=None,
                   update_interface=False,
                   restrict_interface=False):
    '''peek configuration parameters from external pipeline.

    As the paramater dictionary is built at runtime, this method
    executes the pipeline in workingdir, dumping its configuration
    values and reading them into a dictionary.

    If either `pipeline` or `workingdir` are not found, an error is
    raised. This behaviour can be changed by setting `on_error_raise`
    to False. In that case, an empty dictionary is returned.

    Arguments
    ---------
    workingdir : string
       Working directory. This is the directory that the pipeline
       was executed in.
    pipeline : string
       Name of the pipeline script. The pipeline is assumed to live
       in the same directory as the current pipeline.
    on_error_raise : Bool
       If set to a boolean, an error will be raised (or not) if there
       is an error during parameter peeking, for example if
       `workingdir` can not be found. If `on_error_raise` is None, it
       will be set to the default, which is to raise an exception
       unless the calling script is imported or the option
       ``--is-test`` has been passed at the command line.
    prefix : string
       Add a prefix to all parameters. This is useful if the paramaters
       are added to the configuration dictionary of the calling pipeline.
    update_interface : bool
       If True, this method will prefix any options in the
       ``[interface]`` section with `workingdir`. This allows
       transparent access to files in the external pipeline.
    restrict_interface : bool
       If  True, only interface parameters will be imported.

    Returns
    -------
    config : dict
        Dictionary of configuration values.

    '''
    caller_locals = getCallerLocals()

    # check if we should raise errors
    if on_error_raise is None:
        on_error_raise = not isTest() and \
            "__name__" in caller_locals and \
            caller_locals["__name__"] == "__main__"

    # patch - if --help or -h in command line arguments,
    # do not peek as there might be no config file.
    if "--help" in sys.argv or "-h" in sys.argv:
        return {}

    # Attempt to locate directory with pipeline source code. This is a
    # patch as pipelines might be called within the repository
    # directory or from an installed location
    dirname = os.path.dirname(__file__)

    # called without a directory, use current directory
    if dirname == "":
        dirname = os.path.abspath(".")
    else:
        # else: use location of Pipeline.py
        # remove CGAT part, add CGATPipelines
        dirname = os.path.join(os.path.dirname(dirname),
                               "CGATPipelines")
        # if not exists, assume we want version located
        # in directory of calling script.
        if not os.path.exists(dirname):
            # directory is path of calling script
            dirname = os.path.dirname(caller_locals['__file__'])

    pipeline = os.path.join(dirname, pipeline)
    if not os.path.exists(pipeline):
        if on_error_raise:
            raise ValueError(
                "can't find pipeline source %s" % (dirname, pipeline))
        else:
            return {}

    if workingdir == "":
        workingdir = os.path.abspath(".")

    # patch for the "config" target - use default
    # pipeline directory if directory is not specified
    # working dir is set to "?!"
    if "config" in sys.argv or "check" in sys.argv and workingdir == "?!":
        workingdir = os.path.join(CGATPIPELINES_PIPELINE_DIR,
                                  snip(pipeline, ".py"))

    if not os.path.exists(workingdir):
        if on_error_raise:
            raise ValueError(
                "can't find working dir %s" % workingdir)
        else:
            return {}

    statement = "python %s -f -v 0 dump" % pipeline
    process = subprocess.Popen(statement,
                               cwd=workingdir,
                               shell=True,
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)

    # process.stdin.close()
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        raise PipelineError(
            ("Child was terminated by signal %i: \n"
             "The stderr was: \n%s\n") %
            (-process.returncode, stderr))

    for line in stdout.split("\n"):
        if line.startswith("dump"):
            exec(line)

    # update interface
    if update_interface:
        for key, value in dump.items():
            if key.startswith("interface"):
                dump[key] = os.path.join(workingdir, value)

    # keep only interface if so required
    if restrict_interface:
        dump = dict([(k, v) for k, v in dump.iteritems()
                     if k.startswith("interface")])

    # prefix all parameters
    if prefix is not None:
        dump = dict([("%s%s" % (prefix, x), y) for x, y in dump.items()])

    return dump


class MultiLineFormatter(logging.Formatter):
    """add identation for multi-line entries.
    """

    def format(self, record):
        s = logging.Formatter.format(self, record)
        if record.message:
            header, footer = s.split(record.message)
            s = s.replace('\n', '\n' + ' ' * len(header))
        return s


class LoggingFilterRabbitMQ(logging.Filter):
    """pass event information to a rabbitMQ message queue.

    This is a log filter which detects messages from ruffus_ and sends
    them to a rabbitMQ message queue.

    A :term:`task` is a ruffus_ decorated function, which will execute
    one or more :term:`jobs`.

    Valid task/job status:

    update
       task/job needs updating
    completed
       task/job completed successfully
    failed
       task/job failed
    running
       task/job is running
    ignore
       ignore task/job (is up-to-date)

    Arguments
    ---------
    ruffus_text : string
        Log messages from ruffus.pipeline_printout. These are used
        to collect all tasks that will be executed during pipeline
        executation.
    project_name : string
        Name of the project
    pipeline_name : string
        Name of the pipeline
    host : string
        RabbitMQ host name
    exchange : string
        RabbitMQ exchange name

    """

    def __init__(self, ruffus_text,
                 project_name,
                 pipeline_name,
                 host="localhost",
                 exchange="ruffus_pipelines"):

        self.project_name = project_name
        self.pipeline_name = pipeline_name
        self.exchange = exchange

        # dictionary of jobs to run
        self.jobs = {}
        self.tasks = {}

        if not HAS_PIKA:
            self.connected = False
            return

        def split_by_job(text):
            text = "".join(text)
            job_message = ""
            # ignore first entry which is the docstring
            for line in text.split(" Job  = ")[1:]:
                try:
                    # long file names cause additional wrapping and
                    # additional white-space characters
                    job_name = re.search(
                        "\[.*-> ([^\]]+)\]", line).groups()
                except AttributeError:
                    raise AttributeError("could not parse '%s'" % line)
                job_status = "ignore"
                if "Job needs update" in line:
                    job_status = "update"

                yield job_name, job_status, job_message

        def split_by_task(text):
            block, task_name = [], None
            task_status = None
            for line in text.split("\n"):
                if line.startswith("Tasks which will be run"):
                    task_status = "update"
                elif line.startswith("Tasks which are up-to-date"):
                    task_status = "ignore"

                if line.startswith("Task = "):
                    if task_name:
                        yield task_name, task_status, list(split_by_job(block))
                    block = []
                    task_name = re.match("Task = (.*)", line).groups()[0]
                    continue
                if line:
                    block.append(line)
            if task_name:
                yield task_name, task_status, list(split_by_job(block))

        # create connection
        try:
            connection = pika.BlockingConnection(pika.ConnectionParameters(
                host=host))
            self.connected = True
        except pika.exceptions.AMQPConnectionError:
            self.connected = False
            return

        self.channel = connection.channel()
        self.channel.exchange_declare(
            exchange=self.exchange,
            type='topic')

        # populate with initial messages
        for task_name, task_status, jobs in split_by_task(ruffus_text):
            if task_name.startswith("(mkdir"):
                continue

            to_run = 0
            for job_name, job_status, job_message in jobs:
                self.jobs[job_name] = (task_name, job_name)
                if job_status == "update":
                    to_run += 1

            self.tasks[task_name] = [task_status, len(jobs),
                                     len(jobs) - to_run]
            self.send_task(task_name)

    def send_task(self, task_name):
        '''send task status.'''

        if not self.connected:
            return

        task_status, task_total, task_completed = self.tasks[task_name]

        data = {}
        data['created_at'] = time.time()
        data['pipeline'] = self.pipeline_name
        data['task_name'] = task_name
        data['task_status'] = task_status
        data['task_total'] = task_total
        data['task_completed'] = task_completed

        key = "%s.%s.%s" % (self.project_name, self.pipeline_name, task_name)

        try:
            self.channel.basic_publish(exchange=self.exchange,
                                       routing_key=key,
                                       body=json.dumps(data))
        except pika.exceptions.ConnectionClosed:
            L.warn("could not send message - connection closed")
        except Exception as e:
            L.warn("could not send message: %s" % str(e))

    def send_error(self, task_name, job, error=None, msg=None):

        if not self.connected:
            return

        try:
            task_status, task_total, task_completed = self.tasks[task_name]
        except KeyError:
            L.warn("could not get task information for %s, no message sent" %
                   task_name)
            return

        data = {}
        data['created_at'] = time.time()
        data['pipeline'] = self.pipeline_name
        data['task_name'] = task_name
        data['task_status'] = 'failed'
        data['task_total'] = task_total
        data['task_completed'] = task_completed

        key = "%s.%s.%s" % (self.project_name, self.pipeline_name, task_name)

        try:
            self.channel.basic_publish(exchange=self.exchange,
                                       routing_key=key,
                                       body=json.dumps(data))
        except pika.exceptions.ConnectionClosed:
            L.warn("could not send message - connection closed")
        except Exception as e:
            L.warn("could not send message: %s" % str(e))

    def filter(self, record):

        if not self.connected:
            return True

        # filter ruffus logging messages
        if record.filename.endswith("task.py"):
            try:
                before, task_name = record.msg.split(" = ")
            except ValueError:
                return True

            # ignore the mkdir, etc tasks
            if task_name not in self.tasks:
                return True

            if before == "Task enters queue":
                self.tasks[task_name][0] = "running"
            elif before == "Completed Task":
                self.tasks[task_name][0] = "completed"
            elif before == "Uptodate Task":
                self.tasks[task_name][0] = "uptodate"
            else:
                return True

            # send new task status out
            self.send_task(task_name)

        return True


USAGE = '''
usage: %prog [OPTIONS] [CMD] [target]

Execute pipeline %prog.

Commands can be any of the following

make <target>
   run all tasks required to build *target*

show <target>
   show tasks required to build *target* without executing them

plot <target>
   plot image (using inkscape) of pipeline state for *target*

debug <target> [args]
   debug a method using the supplied arguments. The method <target>
   in the pipeline is run without checking any dependencies.

config
   write new configuration files pipeline.ini, sphinxreport.ini and conf.py
   with default values

dump
   write pipeline configuration to stdout

touch
   touch files only, do not run

regenerate
   regenerate the ruffus checkpoint file

check
   check if requirements (external tool dependencies) are satisfied.

clone <source>
   create a clone of a pipeline in <source> in the current
   directory. The cloning process aims to use soft linking to files
   (not directories) as much as possible.  Time stamps are
   preserved. Cloning is useful if a pipeline needs to be re-run from
   a certain point but the original pipeline should be preserved.

'''


def main(args=sys.argv):
    """command line control function for a pipeline.

    This method defines command line options for the pipeline and
    updates the global configuration dictionary correspondingly.

    It then provides a command parser to execute particular tasks
    using the ruffus pipeline control functions. See the generated
    command line help for usage.

    To use it, add::

        import CGAT.Pipeline as P

        if __name__ == "__main__":
            sys.exit(P.main(sys.argv))

    to your pipeline script.

    Arguments
    ---------
    args : list
        List of command line arguments.

    """

    global GLOBAL_OPTIONS
    global GLOBAL_ARGS
    global GLOBAL_SESSION

    parser = E.OptionParser(version="%prog version: $Id$",
                            usage=USAGE)

    parser.add_option("--pipeline-action", dest="pipeline_action",
                      type="choice",
                      choices=(
                          "make", "show", "plot", "dump", "config", "clone",
                          "check", "regenerate"),
                      help="action to take [default=%default].")

    parser.add_option("--pipeline-format", dest="pipeline_format",
                      type="choice",
                      choices=("dot", "jpg", "svg", "ps", "png"),
                      help="pipeline format [default=%default].")

    parser.add_option("-n", "--dry-run", dest="dry_run",
                      action="store_true",
                      help="perform a dry run (do not execute any shell "
                      "commands) [default=%default].")

    parser.add_option("-f", "--force-output", dest="force",
                      action="store_true",
                      help="force running the pipeline even if there "
                      "are uncommited changes "
                      "in the repository [default=%default].")

    parser.add_option("-p", "--multiprocess", dest="multiprocess", type="int",
                      help="number of parallel processes to use on "
                      "submit host "
                      "(different from number of jobs to use for "
                      "cluster jobs) "
                      "[default=%default].")

    parser.add_option("-e", "--exceptions", dest="log_exceptions",
                      action="store_true",
                      help="echo exceptions immediately as they occur "
                      "[default=%default].")

    parser.add_option("-i", "--terminate", dest="terminate",
                      action="store_true",
                      help="terminate immediately at the first exception "
                      "[default=%default].")

    parser.add_option("-d", "--debug", dest="debug",
                      action="store_true",
                      help="output debugging information on console, "
                      "and not the logfile "
                      "[default=%default].")

    parser.add_option("-s", "--set", dest="variables_to_set",
                      type="string", action="append",
                      help="explicitely set paramater values "
                      "[default=%default].")

    parser.add_option("-c", "--checksums", dest="checksums",
                      type="int",
                      help="set the level of ruffus checksums"
                      "[default=%default].")

    parser.add_option("-t", "--is-test", dest="is_test",
                      action="store_true",
                      help="this is a test run"
                      "[default=%default].")

    parser.add_option("--rabbitmq-exchange", dest="rabbitmq_exchange",
                      type="string",
                      help="RabbitMQ exchange to send log messages to "
                      "[default=%default].")

    parser.add_option("--rabbitmq-host", dest="rabbitmq_host",
                      type="string",
                      help="RabbitMQ host to send log messages to "
                      "[default=%default].")

    parser.set_defaults(
        pipeline_action=None,
        pipeline_format="svg",
        pipeline_targets=[],
        multiprocess=40,
        logfile="pipeline.log",
        dry_run=False,
        force=False,
        log_exceptions=False,
        exceptions_terminate_immediately=False,
        debug=False,
        variables_to_set=[],
        is_test=False,
        checksums=0,
        rabbitmq_host="saruman",
        rabbitmq_exchange="ruffus_pipelines")

    (options, args) = E.Start(parser,
                              add_cluster_options=True)

    GLOBAL_OPTIONS, GLOBAL_ARGS = options, args
    E.info("Started in: %s" % WORKING_DIRECTORY)
    # At this point, the PARAMS dictionary has already been
    # built. It now needs to be updated with selected command
    # line options as these should always take precedence over
    # configuration files.

    PARAMS["dryrun"] = options.dry_run
    if options.cluster_queue is not None:
        PARAMS["cluster_queue"] = options.cluster_queue
    if options.cluster_priority is not None:
        PARAMS["cluster_priority"] = options.cluster_priority
    if options.cluster_num_jobs is not None:
        PARAMS["cluster_num_jobs"] = options.cluster_num_jobs
    if options.cluster_options is not None:
        PARAMS["cluster_options"] = options.cluster_options
    if options.cluster_parallel_environment is not None:
        PARAMS["cluster_parallel_environment"] =\
            options.cluster_parallel_environment

    for variables in options.variables_to_set:
        variable, value = variables.split("=")
        PARAMS[variable.strip()] = IOTools.str2val(value.strip())

    version = None

    try:
        # this is for backwards compatibility
        # get mercurial version
        repo = hgapi.Repo(PARAMS["pipeline_scriptsdir"])
        version = repo.hg_id()

        status = repo.hg_status()
        if status["M"] or status["A"]:
            if not options.force:
                raise ValueError(
                    ("uncommitted change in code "
                     "repository at '%s'. Either commit or "
                     "use --force-output") % PARAMS["pipeline_scriptsdir"])
            else:
                E.warn("uncommitted changes in code repository - ignored ")
        version = version[:-1]
    except:
        # try git:
        try:
            stdout, stderr = execute(
                "git rev-parse HEAD", cwd=PARAMS["pipeline_scriptsdir"])
        except:
            stdout = "NA"
        version = stdout

    if args:
        options.pipeline_action = args[0]
        if len(args) > 1:
            options.pipeline_targets.extend(args[1:])

    if options.pipeline_action == "check":
        counter, requirements = Requirements.checkRequirementsFromAllModules()
        for requirement in requirements:
            E.info("\t".join(map(str, requirement)))
        E.info("version check summary: %s" % str(counter))
        E.Stop()
        return

    elif options.pipeline_action == "debug":
        # create the session proxy
        GLOBAL_SESSION = drmaa.Session()
        GLOBAL_SESSION.initialize()

        method_name = options.pipeline_targets[0]
        caller = getCaller()
        method = getattr(caller, method_name)
        method(*options.pipeline_targets[1:])

    elif options.pipeline_action in ("make", "show", "svg", "plot",
                                     "touch", "regenerate"):

        # set up extra file logger
        handler = logging.FileHandler(filename=options.logfile,
                                      mode="a")
        handler.setFormatter(
            MultiLineFormatter(
                '%(asctime)s %(levelname)s %(module)s.%(funcName)s.%(lineno)d %(message)s'))
        logger = logging.getLogger()
        logger.addHandler(handler)
        messenger = None

        try:
            if options.pipeline_action == "make":

                # get tasks to be done. This essentially replicates
                # the state information within ruffus.
                stream = StringIO()
                pipeline_printout(
                    stream,
                    options.pipeline_targets,
                    verbose=5,
                    checksum_level=options.checksums)

                messenger = LoggingFilterRabbitMQ(
                    stream.getvalue(),
                    project_name=getProjectName(),
                    pipeline_name=getPipelineName(),
                    host=options.rabbitmq_host,
                    exchange=options.rabbitmq_exchange)

                logger.addFilter(messenger)

                if not options.without_cluster:
                    global task
                    # use threading instead of multiprocessing in order to
                    # limit the number of concurrent jobs by using the
                    # GIL
                    #
                    # Note that threading might cause problems with rpy.
                    task.Pool = ThreadPool

                    # create the session proxy
                    GLOBAL_SESSION = drmaa.Session()
                    GLOBAL_SESSION.initialize()

                #
                #   make sure we are not logging at the same time in
                #   different processes
                #
                # session_mutex = manager.Lock()
                L.info(E.GetHeader())
                L.info("code location: %s" % PARAMS["pipeline_scriptsdir"])
                L.info("code version: %s" % version)
                L.info("Working directory is: %s" % WORKING_DIRECTORY)

                pipeline_run(
                    options.pipeline_targets,
                    multiprocess=options.multiprocess,
                    logger=logger,
                    verbose=options.loglevel,
                    log_exceptions=options.log_exceptions,
                    exceptions_terminate_immediately=options.exceptions_terminate_immediately,
                    checksum_level=options.checksums,
                )

                L.info(E.GetFooter())

                if GLOBAL_SESSION is not None:
                    GLOBAL_SESSION.exit()

            elif options.pipeline_action == "show":
                pipeline_printout(
                    options.stdout,
                    options.pipeline_targets,
                    verbose=options.loglevel,
                    checksum_level=options.checksums)

            elif options.pipeline_action == "touch":
                pipeline_run(
                    options.pipeline_targets,
                    touch_files_only=True,
                    verbose=options.loglevel,
                    checksum_level=options.checksums)

            elif options.pipeline_action == "regenerate":
                pipeline_run(
                    options.pipeline_targets,
                    touch_files_only=options.checksums,
                    verbose=options.loglevel)

            elif options.pipeline_action == "svg":
                pipeline_printout_graph(
                    options.stdout,
                    options.pipeline_format,
                    options.pipeline_targets,
                    checksum_level=options.checksums)

            elif options.pipeline_action == "plot":
                outf, filename = tempfile.mkstemp()
                pipeline_printout_graph(
                    os.fdopen(outf, "w"),
                    options.pipeline_format,
                    options.pipeline_targets,
                    checksum_level=options.checksums)
                execute("inkscape %s" % filename)
                os.unlink(filename)

        except ruffus_exceptions.RethrownJobError, value:

            if not options.debug:
                E.error("%i tasks with errors, please see summary below:" %
                        len(value.args))
                for idx, e in enumerate(value.args):
                    task, job, error, msg, traceback = e

                    if task is None:
                        # this seems to be errors originating within ruffus
                        # such as a missing dependency
                        # msg then contains a RethrownJobJerror
                        msg = str(msg)
                        pass
                    else:
                        task = re.sub("__main__.", "", task)
                        job = re.sub("\s", "", job)

                    if messenger:
                        messenger.send_error(task, job, error, msg)

                    # display only single line messages
                    if len([x for x in msg.split("\n") if x != ""]) > 1:
                        msg = ""

                    E.error("%i: Task=%s Error=%s %s: %s" %
                            (idx, task, error, job, msg))

                E.error("full traceback is in %s" % options.logfile)

                # write full traceback to log file only by removing the stdout
                # handler
                lhStdout = logger.handlers[0]
                logger.removeHandler(lhStdout)
                logger.error("start of error messages")
                logger.error(value)
                logger.error("end of error messages")
                logger.addHandler(lhStdout)

                # raise error
                raise ValueError(
                    "pipeline failed with %i errors" % len(value.args))
            else:
                raise

    elif options.pipeline_action == "dump":
        # convert to normal dictionary (not defaultdict) for parsing purposes
        # do not change this format below as it is exec'd in peekParameters()
        print "dump = %s" % str(dict(PARAMS))

    elif options.pipeline_action == "config":
        f = sys._getframe(1)
        caller = inspect.getargvalues(f).locals["__file__"]
        prefix = os.path.splitext(caller)[0]
        writeConfigFiles(prefix)

    elif options.pipeline_action == "clone":
        clonePipeline(options.pipeline_targets[0])

    else:
        raise ValueError("unknown pipeline action %s" %
                         options.pipeline_action)

    E.Stop()


def _pickle_args(args, kwargs):
    ''' Pickle a set of function arguments. Removes any kwargs that are
    arguements to submit first. Returns a tuple, the first member of which
    is the key word arguements to submit, the second is a file name
    with the picked call arguements '''

    use_args = ["to_cluster",
                "logfile",
                "job_options",
                "job_queue",
                "job_threads",
                "job_memory"]

    submit_args = {}

    for arg in use_args:
        if arg in kwargs:
            submit_args[arg] = kwargs[arg]
            del kwargs[arg]

    args_file = getTempFilename(shared=True)
    pickle.dump([args, kwargs], open(args_file, "wb"))
    return (submit_args, args_file)


if __name__ == "__main__":
    main()
