################################################################################
#
#   MRC FGU Computational Genomics Group
#
#   $Id: script_template.py 2871 2010-03-03 10:20:44Z andreas $
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
#################################################################################
'''
bam2geneprofile.py - build coverage profile for a set of transcripts/genes
===========================================================================

:Author: Andreas Heger
:Release: $Id$
:Date: |today|
:Tags: Python

Purpose
-------

This script takes a :term:`gtf` formatted file and computes density profiles
over various annotations.


.. todo::
   
   paired-endedness is not fully implemented.

Usage
-----

Example::

   python script_template.py --help

Type::

   python script_template.py --help

for command line help.

Documentation
-------------

For read counts to be correct the NH flag to be set correctly.

Code
----

'''

import os, sys, re, optparse, collections
import Experiment as E
import IOTools
import pysam
import GTF
import numpy

import pyximport
pyximport.install(build_in_temp=False)
import _bam2geneprofile

def main( argv = None ):
    """script main.

    parses command line options in sys.argv, unless *argv* is given.
    """

    if not argv: argv = sys.argv

    # setup command line parser
    parser = optparse.OptionParser( version = "%prog version: $Id: script_template.py 2871 2010-03-03 10:20:44Z andreas $", 
                                    usage = globals()["__doc__"] )

    parser.add_option( "-m", "--method", dest="methods", type = "choice", action = "append",
                       choices = ("geneprofile", "tssprofile" ),
                       help = "counters to use. "
                              "[%default]" )

    parser.add_option( "-n", "--normalization", dest="normalization", type = "choice",
                       choices = ("none", "max", "sum", "total-max", "total-sum", "average"  ),
                       help = "counters to use. "
                              "[%default]" )

    parser.add_option( "-r", "--reporter", dest="reporter", type = "choice",
                       choices = ("gene", "transcript"  ),
                       help = "report results for gene or transcript. "
                              "[%default]" )

    parser.add_option( "-i", "--shift", dest="shift", type = "int",
                       help = "shift for reads. "
                              "[%default]" )

    parser.set_defaults(
        remove_rna = False,
        ignore_pairs = False,
        input_reads = 0,
        force_output = False,
        bin_size = 10,
        shift = 0,
        sort = [],
        reporter = "transcript",
        resolution_cds = 1000,
        resolution_upstream_utr = 1000,
        resolution_downstream_utr = 1000,
        resolution_upstream = 1000,
        resolution_downstream = 1000,
        extension_upstream = 1000,
        extension_downstream = 1000,
        extension_inward = 3000,
        extension_outward = 3000,
        plot = True,
        methods = [],
        normalization = None,
        )

    ## add common options (-h/--help, ...) and parse command line 
    (options, args) = E.Start( parser, argv = argv, add_output_options = True )

    if len(args) != 2:
        raise ValueError("please specify a bam or bed file and a gtf file" )

    bamfile, gtffile = args

    if options.reporter == "gene":
        gtf_iterator = GTF.flat_gene_iterator( GTF.iterator( IOTools.openFile( gtffile ) ) )
    elif options.reporter == "transcript":
        gtf_iterator = GTF.transcript_iterator( GTF.iterator( IOTools.openFile( gtffile ) ) )

    if bamfile.endswith( ".bam" ):
        infile = pysam.Samfile( bamfile, "rb" )
        format = "bam"
        range_counter = _bam2geneprofile.RangeCounterBAM( infile, shift = options.shift )
    elif bamfile.endswith( ".bed.gz" ):
        infile = pysam.Tabixfile( bamfile )
        format = "bed"
        range_counter = _bam2geneprofile.RangeCounterBed( infile )
    else:
        raise NotImplementedError( "can't determine file type for %s" % bamfile )

    counters = []
    for method in options.methods:
        if method == "geneprofile":
            counters.append( _bam2geneprofile.GeneCounter( range_counter, 
                                                           options.resolution_upstream,
                                                           options.resolution_upstream_utr,
                                                           options.resolution_cds,
                                                           options.resolution_downstream_utr,
                                                           options.resolution_downstream,
                                                           options.extension_upstream,
                                                           options.extension_downstream ) )


        elif method == "tssprofile":
            counters.append( _bam2geneprofile.TSSCounter( range_counter, 
                                                           options.extension_outward,
                                                           options.extension_inward ) )

    # set normalization
    for c in counters:
        c.setNormalization( options.normalization )

    E.info( "starting counting with %i counters" % len(counters) )

    _bam2geneprofile.count( counters, gtf_iterator )

    for method, counter in zip(options.methods, counters):
        outfile = IOTools.openFile( E.getOutputFile( counter.name ) + ".tsv.gz", "w")
        counter.writeMatrix( outfile )
        outfile.close()
        
    if options.plot:

        import matplotlib
        # avoid Tk or any X
        matplotlib.use( "Agg" )
        import matplotlib.pyplot as plt
        
        for method, counter in zip(options.methods, counters):
            plt.figure()
            if method == "geneprofile":

                plt.subplots_adjust( wspace = 0.05)
                max_scale = max( [max(x) for x in counter.aggregate_counts ] )

                for x, counts in enumerate( counter.aggregate_counts ):
                    plt.subplot( 5, 1, x+1)
                    plt.plot( range(len(counts)), counts )
                    plt.title( counter.fields[x] )
                    plt.ylim( 0, max_scale )

            elif method == "tssprofile":

                plt.subplot( 1, 3, 1)
                plt.plot( range(-options.extension_outward, options.extension_inward), counter.aggregate_counts[0] )
                plt.title( counter.fields[0] )
                plt.subplot( 1, 3, 2)
                plt.plot( range(-options.extension_inward, options.extension_outward), counter.aggregate_counts[1] )
                plt.title( counter.fields[1] )
                plt.subplot( 1, 3, 3)
                plt.title( "combined" )
                plt.plot( range(-options.extension_outward, options.extension_inward), counter.aggregate_counts[0] )
                plt.plot( range(-options.extension_inward, options.extension_outward), counter.aggregate_counts[1] )
                plt.legend( counter.fields[:2] )

            fn = E.getOutputFile( counter.name ) + ".png"
            plt.savefig( os.path.expanduser(fn) )
        
    ## write footer and output benchmark information.
    E.Stop()

if __name__ == "__main__":
    sys.exit( main( sys.argv) )

    