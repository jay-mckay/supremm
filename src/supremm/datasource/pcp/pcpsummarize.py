#!/usr/bin/env python
""" Summarize module for PCP datasource """

import datetime

from ctypes import c_uint
from pcp import pmapi
import cpmapi as c_pmapi
import time
import logging
import traceback
from supremm.plugin import NodeMetadata
from supremm.rangechange import RangeChange, DataCache
from supremm.summarize import Summarize
from supremm.datasource.pcp.pcpcinterface import pcpcinterface

import numpy
import copy


class ArchiveMeta(NodeMetadata):
    """ container for achive metadata """
    def __init__(self, nodename, nodeidx, archivedata):
        self._nodename = nodename
        self._nodeidx = nodeidx
        self._archivedata = archivedata

    nodename = property(lambda self: self._nodename)
    nodeindex = property(lambda self: self._nodeidx)
    archive = property(lambda self: self._archivedata)

class PCPSummarize(Summarize):
    """
    Summarize class is responsible for iteracting with the pmapi python code
    and managing the calls to the various analytics to process the data
    """

    def __init__(self, preprocessors, analytics, job, config, fail_fast=False):
        super().__init__(preprocessors, analytics, job, config, fail_fast)
        self.start = time.time()
        self.archives_processed = 0
        self.rangechange = RangeChange(config)

    def process(self):
        """ Main entry point. All archives are processed """
        success = 0
        self.archives_processed = 0

        for nodename, nodeidx, archive in self.job.nodearchives():
            try:
                self.processarchive(nodename, nodeidx, archive)
                self.archives_processed += 1
            except pmapi.pmErr as exc:
                success -= 1
                #pylint: disable=not-callable
                self.adderror("archive", "{0}: pmapi.pmErr: {1}".format(archive, exc.message()))

            except Exception as exc:
                success -= 1
                self.adderror("archive", "{0}: Exception: {1}. {2}".format(archive, str(exc), traceback.format_exc()))
                if self.fail_fast:
                    raise

        return success == 0

    def complete(self):
        """ A job is complete if archives exist for all assigned nodes and they have
            been processed sucessfullly
        """
        return self.job.nodecount == self.archives_processed

    def good_enough(self):
        """ A job is good_enough if archives for 95% of nodes have
            been processed sucessfullly
        """
        return self.archives_processed >= 0.95 * float(self.job.nodecount)

    def get(self):
        """ Return a dict with the summary information """
        output = {}
        timeseries = {}

        je = self.job.get_errors()
        if len(je) > 0:
            self.adderror("job", je)

        if self.job.nodecount > 0:
            for analytic in self.alltimestamps:
                if analytic.status != "uninitialized":
                    if analytic.mode == "all":
                        output[analytic.name] = analytic.results()
                    if analytic.mode == "timeseries":
                        timeseries[analytic.name] = analytic.results()
            for analytic in self.firstlast:
                if analytic.status != "uninitialized":
                    output[analytic.name] = analytic.results()

        output['summarization'] = {
            "version": self.version,
            "elapsed": time.time() - self.start,
            "created": time.time(),
            "srcdir": self.job.jobdir,
            "complete": self.complete(),
            "datasource": "pcp"
        }

        output['created'] = datetime.datetime.utcnow()

        output['acct'] = self.job.acct
        output['acct']['id'] = self.job.job_id

        if len(timeseries) > 0:
            timeseries['hosts'] = dict((str(idx), name) for name, idx, _ in self.job.nodearchives())
            timeseries['version'] = self.timeseries_version
            output['timeseries'] = timeseries

        for preproc in self.preprocs:
            result = preproc.results()
            if preproc.status != "uninitialized" and result is not None:
                output.update(result)

        for source, data in self.job.data().items():
            if 'errors' in data:
                self.adderror(source, str(data['errors']))

        if len(self.errors) > 0:
            output['errors'] = {}
            for k, v in self.errors.items():
                output['errors'][k] = list(v)

        return output

    def runcallback(self, analytic, result, mtypes, ctx, mdata, metric_id_array):
        """ get the data and call the analytic """

        def logerr(err):
            self.logerror(mdata.nodename, analytic.name, err)
        data, description = pcpcinterface.extractValues(ctx, result, metric_id_array, mtypes, logerr)

        if data is None and description is None:
            return False
        elif data is True and description is True:
            logging.warning("%s %s missing indom @ %s", mdata.nodename, analytic.name, float(result.contents.timestamp))
            self.logerror(mdata.nodename, analytic.name, "missing indom")
            return True

        try:
            self.rangechange.normalise_data(float(result.contents.timestamp), data)
            retval = analytic.process(mdata, float(result.contents.timestamp), data, description)
            return retval
        except Exception as e:
            logging.exception("%s %s @ %s", self.job.job_id, analytic.name, float(result.contents.timestamp))
            self.logerror(mdata.nodename, analytic.name, str(e))
            return False

    def runpreproccall(self, preproc, result, mtypes, ctx, mdata, metric_id_array):
        """ Call the pre-processor data processing function """

        data, description = pcpcinterface.extractpreprocValues(ctx, result, metric_id_array, mtypes)

        if data is None and description is None:
            return False

        return preproc.process(float(result.contents.timestamp), data, description)

    def processforpreproc(self, ctx, mdata, preproc):
        """ fetch the data from the archive, reformat as a python data structure
        and call the analytic process function """

        preproc.hoststart(mdata.nodename)

        metric_id_array, metricnames = pcpcinterface.getmetricstofetch(ctx, preproc)

        # Range correction is not performed for the pre-processors. They always
        # see the original data
        self.rangechange.set_fetched_metrics([])

        if len(metric_id_array) == 0:
            logging.debug("Skipping %s (%s)" % (type(preproc).__name__, preproc.name))
            preproc.hostend()
            return

        mtypes = pcpcinterface.getmetrictypes(ctx, metric_id_array)

        done = False

        while not done:
            try:
                result = ctx.pmFetch(metric_id_array)

                if False == self.runpreproccall(preproc, result, mtypes, ctx, mdata, metric_id_array):
                    # A return value of false from process indicates the computation
                    # failed and no more data should be sent.
                    done = True

                ctx.pmFreeResult(result)

            except pmapi.pmErr as exp:
                if exp.args[0] == c_pmapi.PM_ERR_EOL:
                    done = True
                else:
                    preproc.status = "failure"
                    preproc.hostend()
                    raise exp
            except Exception as exp:
                preproc.status = "failure"
                preproc.hostend()
                raise exp


        preproc.status = "complete"
        preproc.hostend()

    def processforanalytic(self, ctx, mdata, analytic):
        """ fetch the data from the archive, reformat as a python data structure
        and call the analytic process function """

        metric_id_array, metricnames = pcpcinterface.getmetricstofetch(ctx, analytic)

        if len(metric_id_array) == 0:
            logging.debug("Skipping %s (%s)" % (type(analytic).__name__, analytic.name))
            return

        self.rangechange.set_fetched_metrics(metricnames)

        mtypes = pcpcinterface.getmetrictypes(ctx, metric_id_array)

        done = False

        while not done:
            result = None
            try:
                result = ctx.pmFetch(metric_id_array)

                if False == self.runcallback(analytic, result, mtypes, ctx, mdata, metric_id_array):
                    # A return value of false from process indicates the computation
                    # failed and no more data should be sent.
                    done = True

            except pmapi.pmErr as exp:
                if exp.args[0] == c_pmapi.PM_ERR_EOL:
                    done = True
                else:
                    logging.warning("%s (%s) raised exception %s", type(analytic).__name__, analytic.name, str(exp))
                    analytic.status = "failure"
                    raise exp
            finally:
                if result != None:
                    ctx.pmFreeResult(result)

        analytic.status = "complete"

    def logerror(self, archive, analyticname, pmerrorcode):
        """
        Store the detail of archive processing errors
        """
        #if analyticname != "gpu":
        logging.debug("archive processing exception: %s %s %s", archive, analyticname, pmerrorcode)
        self.adderror("archive", "{0} {1} {2}".format(archive, analyticname, pmerrorcode))

    def processfirstlast(self, ctx, mdata, analytic):
        """ fetch the data from the archive, reformat as a python data structure
        and call the analytic process function """

        metric_id_array, metricnames = pcpcinterface.getmetricstofetch(ctx, analytic)

        if len(metric_id_array) == 0:
            return

        self.rangechange.set_fetched_metrics(metricnames)

        mtypes = pcpcinterface.getmetrictypes(ctx, metric_id_array)

        try:
            result = ctx.pmFetch(metric_id_array)
            firstimestamp = copy.deepcopy(result.contents.timestamp)

            if False == self.runcallback(analytic, result, mtypes, ctx, mdata, metric_id_array):
                analytic.status = "failure"
                ctx.pmFreeResult(result)
                return

            ctx.pmFreeResult(result)
            result = None

            if self.rangechange.passthrough == False:
                # need to process every timestamp and only pass the last one to the plugin
                done = False
                datacache = DataCache()
                while not done:
                    try:
                        result = ctx.pmFetch(metric_id_array)
                        if False == self.runcallback(datacache, result, mtypes, ctx, mdata, metric_id_array):
                            # A return value of false from process indicates the computation
                            # failed and no more data should be sent.
                            done = True
                    except pmapi.pmErr as exp:
                        if exp.args[0] == c_pmapi.PM_ERR_EOL:
                            done = True
                        else:
                            logging.warning("%s (%s) raised exception %s", type(analytic).__name__, analytic.name, str(exp))
                            analytic.status = "failure"
                            raise exp
                    finally:
                        if result != None:
                            ctx.pmFreeResult(result)
                            result = None

                if False == datacache.docallback(analytic):
                    analytic.status = "failure"
                    return

            else:
                ctx.pmSetMode(c_pmapi.PM_MODE_BACK, ctx.pmGetArchiveEnd(), 0)

                result = ctx.pmFetch(metric_id_array)

                if result.contents.timestamp.tv_sec == firstimestamp.tv_sec and result.contents.timestamp.tv_usec == firstimestamp.tv_usec:
                    # This achive must only contain one data point for these metrics
                    ctx.pmFreeResult(result)
                    result = None
                    return

                if False == self.runcallback(analytic, result, mtypes, ctx, mdata, metric_id_array):
                    analytic.status = "failure"
                    ctx.pmFreeResult(result)
                    result = None
                    return

            analytic.status = "complete"

            if result != None:
                ctx.pmFreeResult(result)
                result = None

        except pmapi.pmErr as e:
            if e.args[0] == c_pmapi.PM_ERR_EOL:
                pass
            else:
                logging.exception("%s", analytic.name)
                raise e

    def processarchive(self, nodename, nodeidx, archive):
        """ process the archive """
        # TODO need to benchmark code to see if there is a benefit to interleaving the calls to
        # pmFetch for the different contexts. This version runs all the pmFetches for each analytic
        # in turn.
        context = pmapi.pmContext(c_pmapi.PM_CONTEXT_ARCHIVE, archive)
        mdata = ArchiveMeta(nodename, nodeidx, context.pmGetArchiveLabel())

        for preproc in self.preprocs:
            context.pmSetMode(c_pmapi.PM_MODE_FORW, mdata.archive.start, 0)
            self.processforpreproc(context, mdata, preproc)

        for analytic in self.alltimestamps:
            context.pmSetMode(c_pmapi.PM_MODE_FORW, mdata.archive.start, 0)
            self.processforanalytic(context, mdata, analytic)

        for analytic in self.firstlast:
            context.pmSetMode(c_pmapi.PM_MODE_FORW, mdata.archive.start, 0)
            self.processfirstlast(context, mdata, analytic)
