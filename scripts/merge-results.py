
"""
Build an scratch build in koji base on Pagure pull request
"""
import argparse
import logging
import os
import sys
import traceback
import json
import xml.etree.cElementTree as ET
from xml.dom import minidom
import yaml

this = sys.modules[__name__]

this.task_id = None
this.logger = None

this.result_file = None
this.output_log = None

# pylint: disable=logging-format-interpolation


def configure_logging(verbose=False, output_file=None):
    """Configure logging
    If verbose is set, set debug level for the default console logger
    If output_file is set, the logs are also saved on file
    Return logger object.
    """

    this.logger = logging.getLogger(__name__)

    logger_lvl = logging.INFO

    if verbose:
        logger_lvl = logging.DEBUG

    this.logger.setLevel(logger_lvl)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(levelname)s: %(message)s")
    ch.setFormatter(formatter)
    this.logger.addHandler(ch)

    if output_file:
        if os.path.isfile(output_file):
            os.remove(output_file)
        output_fh = logging.FileHandler(output_file)
        output_fh.setLevel(logger_lvl)
        output_fh.setFormatter(formatter)
        this.logger.addHandler(output_fh)

    # #To allow stdout redirect
    this.logger.write = lambda msg: this.logger.info(msg) if msg != '\n' else None
    this.logger.flush = lambda: None


def merge_results(base_path, output):
    """
    Expects each subdirectory of base_path to contain results.yml
    Creates a new results.yml with all results
    """

    result_dirs = os.listdir(base_path)
    if not result_dirs:
        raise Exception("Couldn't find any subdirectory on {}".format(base_path))

    results = {}
    this.logger.info("Merging results from {}".format(base_path))
    for _dir in result_dirs:
        if not os.path.isdir("{}/{}".format(base_path, _dir)):
            this.logger.debug("skip {} as it is not a directory".format(_dir))
            continue
        filename = "{}/{}/results.yml".format(base_path, _dir)
        if not os.path.isfile(filename):
            raise Exception("Couldn't find {}".format(filename))
        with open(filename, 'r') as _file:
            this.logger.debug("loading {}".format(filename))
            tmp_result = yaml.safe_load(_file)
        if not results:
            results["results"] = []
        this.logger.debug("adding results")
        # update test name and log path to be related to _dir
        for _result in tmp_result["results"]:
            _result["test"] = "{}/{}".format(_dir, _result["test"])
            if "logs" in _result:
                _result["logs"][:] = ["{}/{}".format(_dir, s) for s in _result["logs"]]
        results["results"].extend(tmp_result["results"])

    output_dir = os.path.dirname(output)
    if output_dir and not os.path.isdir(output_dir):
        this.logger.debug("Creating {}".format(output_dir))
        os.makedirs(output_dir)

    this.logger.debug("Collected all results saving it to {}".format(output))
    with open(output, 'w') as _file:
        yaml.dump(results, _file)

    this.logger.info("Merged results saved to {}".format(output))
    return results


def results2xunit(results, logs_base_path, xunitfile):
    """
    Save the merged results to xunit format
    """
    this.logger.info("Creating xunit file with test results")
    root = ET.Element("testsuites")
    ts = ET.SubElement(root, "testsuite", name="dist-git")

    for testcase in results:
        testcase_name = testcase['test']
        testcase_result = testcase['result']
        tc = ET.SubElement(ts, "testcase", name=testcase_name)
        if testcase_result != "pass":
            ET.SubElement(tc, "failure")
        logs = ET.SubElement(tc, "logs")
        if 'logs' not in testcase:
            ET.SubElement(logs, "log", name=testcase_name, href=logs_base_path)
            continue
        for log_name in testcase["logs"]:
            log_path = "{}/{}".format(logs_base_path, log_name)
            ET.SubElement(logs, "log", name=log_name, href=log_path)

    xunit_dir = os.path.dirname(xunitfile)
    if xunit_dir and not os.path.isdir(xunit_dir):
        this.logger.debug("Creating {}".format(xunit_dir))
        os.makedirs(xunit_dir)

    xmlstr = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    with open(xunitfile, "w") as _file:
        _file.write(xmlstr)
    this.logger.info("xunit saved to {}".format(xunitfile))


def main():
    """
    Merge results from all results.yml file
    If set creates a xunit of the results
    """
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--results-path", "-r", dest="results_path", required=True,
                        help="Base path for directory that all subdirectories contain results.yml")
    parser.add_argument("--output", "-o", dest="merged_file", required=True,
                        help="New file with merged results")
    parser.add_argument("--xunit-file", "-x", dest="xunit_file",
                        help="New file with merged results")
    parser.add_argument("--base-logs-url", "-p", dest="base_logs_url",
                        help="Base url to be used as refenrece on xunit logs link")
    parser.add_argument("--logs", "-l", dest="logs", default="./",
                        help="Path where logs will be stored")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(args.logs):
        os.makedirs(args.logs)

    logs = os.path.abspath(args.logs)

    this.result_file = "{}/merge-results.json".format(logs)
    this.output_log = "{}/merge-results.log".format(logs)

    configure_logging(verbose=args.verbose, output_file=this.output_log)

    results = merge_results(args.results_path, args.merged_file)
    if args.xunit_file and results:
        if not args.base_logs_url:
            args.base_logs_url = args.results_path
        results2xunit(results["results"], args.base_logs_url, args.xunit_file)

    this.result = {"status": 0, "output_file": this.task_id, "log": this.output_log}
    with open(this.result_file, "w") as _file:
        json.dump(this.result, _file, indent=4, sort_keys=True, separators=(',', ': '))


if __name__ == "__main__":
    try:
        main()
    except Exception as exception:
        traceback.print_exc()
        this.logger.error(str(exception))
        this.result = {"status": 1, "task_id": this.task_id, "error_reason": str(exception),
                       "log": this.output_log}
        with open(this.result_file, "w") as _file:
            json.dump(this.result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)

    sys.exit(0)
