##########################################################################
#
#   MRC FGU Computational Genomics Analysis & Training Programme
#
#   $Id$
#
#   Copyright (C) 2014 David Sims
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

"""
====================
ReadQc pipeline
====================

:Author: David Sims
:Release: $Id$
:Date: |today|
:Tags: Python

The readqc pipeline imports unmapped reads from one or more
fastq and performs basic quality control steps:

   1. per position quality
   2. per read quality
   3. duplicates

For further details see http://www.bioinformatics.bbsrc.ac.uk/projects/fastqc/



Usage
=====

See :ref:`PipelineSettingUp` and :ref:`PipelineRunning`
on general information how to use CGAT pipelines.

Configuration
-------------

No general configuration required.

Input
-----

Reads are imported by placing files or linking to files in the :term:
`working directory`.

The default file format assumes the following convention:

   <sample>-<condition>-<replicate>.<suffix>

``sample`` and ``condition`` make up an :term:`experiment`,
while ``replicate`` denotes the :term:`replicate` within an :term:`experiment`.
The ``suffix`` determines the file type.
The following suffixes/file types are possible:

sra
   Short-Read Archive format. Reads will be extracted using the :file:
   `fastq-dump` tool.

fastq.gz
   Single-end reads in fastq format.

fastq.1.gz, fastq2.2.gz
   Paired-end reads in fastq format.
   The two fastq files must be sorted by read-pair.

.. note::

   Quality scores need to be of the same scale for all input files.
   Thus it might be difficult to mix different formats.

Pipeline output
----------------

The major output is a set of HTML pages and plots reporting on the quality of
the sequence archive

Example
=======

Example data is available at
http://www.cgat.org/~andreas/sample_data/pipeline_readqc.tgz.
To run the example, simply unpack and untar::

   wget http://www.cgat.org/~andreas/sample_data/pipeline_readqc.tgz
   tar -xvzf pipeline_readqc.tgz
   cd pipeline_readqc
   python <srcdir>/pipeline_readqc.py make full

Requirements
==============
+--------------------+---------------+---------------------------------+
|*Program*           |*Version*      |*Purpose*                        |
+--------------------+---------------+---------------------------------+
|fastqc              |?              |read qc                          |
+--------------------+---------------+---------------------------------+
|sickle              |1.33           |Read trimming by quality score   |
+--------------------+---------------+---------------------------------+

Code
====

"""

#########################################################################
#########################################################################
#########################################################################
# load modules


# import ruffus
from ruffus import *

# import useful standard python modules
import sys
import os
import re
import glob
import cStringIO
import numpy
import pandas
from pandas import DataFrame
from scipy.stats import linregress
import itertools as iter

# import modules from the CGAT code collection
import CGAT.Experiment as E
import CGAT.IOTools as IOTools
import CGATPipelines.PipelineMapping as PipelineMapping
import CGATPipelines.PipelineTracks as PipelineTracks
import CGAT.Pipeline as P
import CGAT.CSV2DB as CSV2DB
import CGATPipelines.PipelineReadqc as rqc

#########################################################################
#########################################################################
#########################################################################
# load options from the config file

P.getParameters(
    ["%s/pipeline.ini" % os.path.splitext(__file__)[0],
     "../pipeline.ini",
     "pipeline.ini"])
PARAMS = P.PARAMS

#########################################################################
#########################################################################
#########################################################################
# define input files


INPUT_FORMATS = ("*.fastq.1.gz", "*.fastq.gz", "*.sra", "*.csfasta.gz")
REGEX_FORMATS = regex(r"(\S+).(fastq.1.gz|fastq.gz|sra|csfasta.gz)")


#########################################################################
#########################################################################
#########################################################################
# Get TRACKS grouped on either Sample3 or Sample4 track ids

Sample = PipelineTracks.AutoSample
TRACKS = PipelineTracks.Tracks(Sample).loadFromDirectory(
    files = glob.glob("./*fastq.1.gz") 
    + glob.glob("./*fastq.gz") 
    + glob.glob("./*sra") 
    + glob.glob("./*csfasta.gz"), 
    pattern = "(\S+).(fastq.1.gz|fastq.gz|sra|csfasta.gz)")
if len(TRACKS.getTracks()[0].asList()) == 4:
    EXPERIMENTS = PipelineTracks.Aggregate(TRACKS, labels=("attribute0", 
                                                           "attribute1", 
                                                           "attribute2"))
    TISSUES = PipelineTracks.Aggregate(TRACKS, labels=("attribute1",))
    CONDITIONS = PipelineTracks.Aggregate(TRACKS, labels=( "attribute2",))

elif len(TRACKS.getTracks()[0].asList()) == 3:
    EXPERIMENTS = PipelineTracks.Aggregate(TRACKS, labels=("attribute0", 
                                                           "attribute1"))
    TISSUES = PipelineTracks.Aggregate(TRACKS, labels=("attribute0",))
    CONDITIONS = PipelineTracks.Aggregate(TRACKS, labels=( "attribute1",))
else:
    raise ValueError("Unrecognised PipelineTracks.AutoSample instance")


#########################################################################
#########################################################################
#########################################################################
# Run Fastqc on each input file


@follows(mkdir(PARAMS["exportdir"]), mkdir(os.path.join(PARAMS["exportdir"],
                                                        "fastqc")))
@transform(INPUT_FORMATS,
           REGEX_FORMATS,
           r"\1.fastqc")
def runFastqc(infiles, outfile):
    '''convert sra files to fastq and check mapping qualities are in solexa format.
    Perform quality control checks on reads from .fastq files.'''
    m = PipelineMapping.FastQc(nogroup=PARAMS["readqc_no_group"],
                               outdir=PARAMS["exportdir"] + "/fastqc")
    statement = m.build((infiles,), outfile)
    P.run()

#########################################################################
# parse results files and load into database


@jobs_limit(1, "db")
@transform(runFastqc, suffix(".fastqc"), "_fastqc.load")
def loadFastqc(infile, outfile):
    '''load FASTQC stats.'''
    track = P.snip(infile, ".fastqc")
    filename = os.path.join(
        PARAMS["exportdir"], "fastqc", track + "*_fastqc", "fastqc_data.txt")
    rqc.loadFastqc(filename)
    P.touch(outfile)

#########################################################################


@merge(runFastqc, "status_summary.tsv.gz")
def buildFastQCSummaryStatus(infiles, outfile):
    '''load fastqc status summaries into a single table.'''
    exportdir = os.path.join(PARAMS["exportdir"], "fastqc")
    rqc.buildFastQCSummaryStatus(infiles, outfile, exportdir)

#########################################################################


@merge(runFastqc, "basic_statistics_summary.tsv.gz")
def buildFastQCSummaryBasicStatistics(infiles, outfile):
    '''load fastqc summaries into a single table.'''
    exportdir = os.path.join(PARAMS["exportdir"], "fastqc")
    rqc.buildFastQCSummaryBasicStatistics(infiles, outfile, exportdir)

#########################################################################


regex_exp = "|".join([x.__str__()[:-len("-agg")] for x in EXPERIMENTS])
@follows(mkdir("experiment.dir"))
@collate(runFastqc, 
         regex("(" + regex_exp + ").+"),
         r"experiment.dir/\1_per_sequence_quality.tsv")
def buildExperimentLevelReadQuality(infiles, outfile):
    """
    Collate per sequence read qualities for all samples in EXPERIMENT
    """
    exportdir = os.path.join(PARAMS["exportdir"], "fastqc")
    rqc.buildExperimentReadQuality(infiles, outfile, exportdir)


@collate(buildExperimentLevelReadQuality,
         regex("(.+)/(.+)_per_sequence_quality.tsv"),
         r"\1/experiment_per_sequence_quality.tsv")
def combineExperimentLevelReadQualities(infiles, outfile):
    """
    Combine summaries of read quality for different experiments
    """
    infiles = " ".join(infiles)
    statement = ("python %(scriptsdir)s/combine_tables.py"
                 "  --log=%(outfile)s.log"
                 "  --regex-filename='.+/(.+)_per_sequence_quality.tsv'"
                 " %(infiles)s"
                 " > %(outfile)s")
    P.run()


@transform(combineExperimentLevelReadQualities,
           regex(".+/(.+).tsv"),
           r"\1.load")
def loadExperimentLevelReadQualities(infile, outfile):
    P.load(infile, outfile)

#########################################################################


@transform((buildFastQCSummaryStatus, buildFastQCSummaryBasicStatistics),
           suffix(".tsv.gz"), ".load")
def loadFastqcSummary(infile, outfile):
    P.load(infile, outfile, options="--add-index=track")


#########################################################################


@follows(loadFastqc, loadFastqcSummary)
def full():
    pass


#########################################################################


@follows()
def publish():
    '''publish files.'''
    P.publish_report()


@follows(mkdir("report"))
def build_report():
    '''build report from scratch.'''

    E.info("starting documentation build process from scratch")
    P.run_report(clean=True)


@follows(mkdir("report"))
def update_report():
    '''update report.'''

    E.info("updating documentation")
    P.run_report(clean=False)

if __name__ == "__main__":
    sys.exit(P.main(sys.argv))
