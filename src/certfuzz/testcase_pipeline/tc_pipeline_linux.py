'''
Created on Jul 16, 2014

@organization: cert.org
'''
import logging
import os

from certfuzz.analyzers import cw_gmalloc
from certfuzz.analyzers import pin_calltrace
from certfuzz.analyzers import stderr
from certfuzz.analyzers import valgrind
from certfuzz.analyzers.callgrind import callgrind
from certfuzz.analyzers.callgrind.annotate import annotate_callgrind
from certfuzz.analyzers.callgrind.annotate import annotate_callgrind_tree
from certfuzz.analyzers.callgrind.errors import CallgrindAnnotateEmptyOutputFileError
from certfuzz.analyzers.callgrind.errors import CallgrindAnnotateMissingInputFileError
from certfuzz.file_handlers.watchdog_file import touch_watchdog_file
from certfuzz.fuzztools import filetools
from certfuzz.fuzztools.state_timer import STATE_TIMER
from certfuzz.minimizer import MinimizerError, UnixMinimizer as Minimizer
from certfuzz.testcase_pipeline.tc_pipeline_base import TestCasePipelineBase


logger = logging.getLogger(__name__)


def get_uniq_logger(logfile):
    l = logging.getLogger('uniq_crash')
    if len(l.handlers) == 0:
        hdlr = logging.FileHandler(logfile)
        l.addHandler(hdlr)
    return l


class LinuxTestCasePipeline(TestCasePipelineBase):

    def _setup_analyzers(self):
        self.analyzer_classes.append(stderr.StdErr)
        self.analyzer_classes.append(cw_gmalloc.CrashWranglerGmalloc)

        if self.options.get('use_valgrind'):
            self.analyzer_classes.append(valgrind.Valgrind)
            self.analyzer_classes.append(callgrind.Callgrind)

        if self.options.get('use_pin_calltrace'):
            self.analyzer_classes.append(pin_calltrace.Pin_calltrace)

    def _verify(self, testcase):
        '''
        Confirms that a test case is interesting enough to pursue further analysis
        :param testcase:
        '''
        STATE_TIMER.enter_state('verify_testcase')
        TestCasePipelineBase._verify(self, testcase)

        # if you find more testcases, append them to self.tc_candidate_q
        # tc_verified_q crashes append to self.tc_verified_q

        logger.debug('verifying crash')
        with testcase as tc:
            if tc.is_crash:

                is_new_to_campaign = self.uniq_func(tc.signature)

                # fall back to checking if the crash directory exists
                #
                crash_dir_found = filetools.find_or_create_dir(tc.result_dir)

                tc.should_proceed_with_analysis = is_new_to_campaign and not crash_dir_found

                if tc.should_proceed_with_analysis:
                    logger.info('%s first seen at %d', tc.signature, tc.seednum)
                    self.dbg_out_file_orig = tc.dbg.file
                    logger.debug('Original debugger file: %s', self.dbg_out_file_orig)
                    self.success = True
                else:
                    logger.debug('%s was found, not unique', tc.signature)

    def _post_verify(self, testcase):
        testcase.get_logger()

    def _minimize(self, testcase):
        if self.options.get('minimize_crashers'):
            self._mininimize_to_seedfile(testcase)
        if self.options.get('minimize_to_string'):
            self._minimize_to_string(testcase)

    def _post_minimize(self, testcase):
        pass
        # TODO
#        if self.cfg.recycle_crashers:
#            logger.debug('Recycling crash as seedfile')
#            iterstring = testcase.fuzzedfile.basename.split('-')[1].split('.')[0]
#            crasherseedname = 'sf_' + testcase.seedfile.md5 + '-' + iterstring + testcase.seedfile.ext
#            crasherseed_path = os.path.join(self.cfg.seedfile_origin_dir, crasherseedname)
#            filetools.copy_file(testcase.fuzzedfile.path, crasherseed_path)
#            seedfile_set.add_file(crasherseed_path)

    def _pre_analyze(self, testcase):
        STATE_TIMER.enter_state('analyze_testcase')

        # get one last debugger output for the newly minimized file
        if testcase.pc_in_function:
            # change the debugger template
            testcase.set_debugger_template('complete')
        else:
            # use a debugger template that specifies fixed offsets from $pc for disassembly
            testcase.set_debugger_template('complete_nofunction')
        logger.info('Getting complete debugger output for crash: %s', testcase.fuzzedfile.path)
        testcase.get_debug_output(testcase.fuzzedfile.path)

        if self.dbg_out_file_orig != testcase.dbg.file:
            # we have a new debugger output
            # remove the old one
            filetools.delete_files(self.dbg_out_file_orig)
            if os.path.exists(self.dbg_out_file_orig):
                logger.warning('Failed to remove old debugger file %s', self.dbg_out_file_orig)
            else:
                logger.debug('Removed old debug file %s', self.dbg_out_file_orig)

    def _analyze(self, testcase):
        # we'll just use the implementation in our parent class
        TestCasePipelineBase._analyze(self, testcase)

    def _post_analyze(self, testcase):
        logger.info('Annotating callgrind output')
        try:
            annotate_callgrind(testcase)
            annotate_callgrind_tree(testcase)
        except CallgrindAnnotateEmptyOutputFileError:
            logger.warning('Unexpected empty output from annotate_callgrind. Continuing')
        except CallgrindAnnotateMissingInputFileError:
            logger.warning('Missing callgrind output. Continuing')

    def _pre_report(self, testcase):
        uniqlogger = get_uniq_logger(self.options.get('uniq_log'))
        uniqlogger.info('%s crash_id=%s seed=%d range=%s bitwise_hd=%d bytewise_hd=%d', testcase.seedfile.basename, testcase.signature, testcase.seednum, testcase.range, testcase.hd_bits, testcase.hd_bytes)
        logger.info('%s first seen at %d', testcase.signature, testcase.seednum)

        # whether it was unique or not, record some details for posterity
        # record the details of this crash so we can regenerate it later if needed
        testcase.logger.info('seen in seedfile=%s at seed=%d range=%s outfile=%s', testcase.seedfile.basename, testcase.seednum, testcase.range, testcase.fuzzedfile.path)
        testcase.logger.info('PC=%s', testcase.pc)

    def _report(self, testcase):
        # TODO move BffCrash.copy_files into this module
        testcase.copy_files()

    def _post_report(self, testcase):
        # always clean up after yourself
        testcase.clean_tmpdir()
        # clean up
        testcase.delete_files()

    def _mininimize_to_seedfile(self, testcase):
        self._minimize_generic(testcase, sftarget=True, confidence=0.999)
        # calculate the hamming distances for this crash
        # between the original seedfile and the minimized fuzzed file
        testcase.calculate_hamming_distances()

    def _minimize_to_string(self, testcase):
        self._minimize_generic(testcase, sftarget=False, confidence=0.9)

    def _minimize_generic(self, testcase, sftarget=True, confidence=0.999):
        touch_watchdog_file()

        STATE_TIMER.enter_state('minimize_testcase')
        try:
            with Minimizer(cfg=self.cfg,
                           crash=testcase,
                           bitwise=False,
                           seedfile_as_target=sftarget,
                           confidence=confidence,
                           tempdir=self.options.get('local_dir'),
                           maxtime=self.options.get('minimizertimeout'),
                           ) as m:
                m.go()
                for new_tc in m.other_crashes.values():
                    self.tc_candidate_q.put(new_tc)
        except MinimizerError as e:
            logger.warning('Unable to minimize %s, proceeding with original fuzzed crash file: %s', testcase.signature, e)
            m = None
