
"""
Build an scratch build in koji base on Pagure pull request
"""
import argparse
import logging
import os
import sys
import traceback
import re
import glob
import subprocess
import json
import time

from contextlib import redirect_stdout
import requests

import koji
from koji_cli.lib import (
    watch_tasks,
    # _progress_callback,
    unique_path
)

global logger
global task_id
logger = None
task_id = None

result_file = "{}/create-build-result.json".format(os.getcwd())
output_log = "{}/create-build.log".format(os.getcwd())

#pylint: disable=logging-format-interpolation

requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning) #pylint: disable=no-member

def _query_url(url, retry=10):
    while retry > 0:
        try:
            resp = requests.get(url, verify=False)
        except Exception:
            retry -= 1
            time.sleep(1)
            continue
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        return resp.text
    print("Could not connect to %s" % url)
    return None

def _rawhide_dist_number():
    url = "https://src.fedoraproject.org/rpms/fedora-release/raw/master/f/fedora-release.spec"
    response = _query_url(url)
    for line in response.split("\n"):
        match = re.match(r"%define dist_version\s+(\S+)$", line)
        if match:
            return match.group(1)
    return None

def configure_logging(verbose=False, output_file=None):
    """Configure logging
    If verbose is set, set debug level for the default console logger
    If output_file is set, the logs are also saved on file
    Return logger object.
    """

    global logger
    logger = logging.getLogger(__name__)

    logger_lvl = logging.INFO

    if verbose:
        logger_lvl = logging.DEBUG

    logger.setLevel(logger_lvl)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(levelname)s: %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)


    if output_file:
        if os.path.isfile(output_file):
            os.remove(output_file)
        output_fh = logging.FileHandler(output_file)
        output_fh.setLevel(logger_lvl)
        output_fh.setFormatter(formatter)
        logger.addHandler(output_fh)

    # #To allow stdout redirect
    logger.write = lambda msg: logger.info(msg) if msg != '\n' else None
    logger.flush = lambda: None


class Koji():
    """
    Class to handle operations on koji
    """
    def __init__(self):
        self.hub = koji.ClientSession("https://koji.fedoraproject.org/kojihub")
        keytab = os.getenv('KOJI_KEYTAB')
        if keytab:
            logger.debug("Authenticating to keytab")
            try:
                self.hub.gssapi_login(keytab=keytab)
            except Exception as exception:
                logger.err(str(exception))
                raise Exception("Couldn't authenticate using keytab")


    def create_build(self, repo, dist_ver, release):
        """
        Build scratch build from specific repo
        """
        logger.info("Bulding src...")
        current_dir = os.getcwd()
        os.chdir(repo)
        cmd = "fedpkg --release {} srpm".format(dist_ver)
        logger.debug("Running {}".format(cmd))
        try:
            subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            logger.error(str(exception))
            if exception.stderr:
                logger.debug(exception.stderr)
            if exception.stdout:
                logger.debug(exception.stdout)
            os.chdir(current_dir)
            raise Exception("Couldn't create src.rpm") from None

        srpms = glob.glob("*.src.rpm")
        if not srpms:
            os.chdir(current_dir)
            raise Exception("Couldn't find src.rpm file")

        source = srpms[0]
        logger.debug("Going to upload {}".format(source))
        serverdir = unique_path('cli-build')
        # callback = _progress_callback
        callback = None
        logger.debug("uploading {} to {}".format(source, serverdir))
        max_retry = 5
        attempt = 1
        while True:
            try:
                self.hub.uploadWrapper(source, serverdir, callback=callback)
            except Exception as exception:
                attempt += 1
                if attempt > max_retry:
                    logger.error(str(exception))
                    os.chdir(current_dir)
                    raise Exception("Failed uploading {}".format(source)) from None
                time.sleep(10)
                logger.info("Retrying to upload {}. Attempt {}/{}".format(source, attempt, max_retry))
                continue
            break
        source = "%s/%s" % (serverdir, os.path.basename(source))

        logger.info("Building scratch build for {}".format(source))
        opts = {"scratch": True, "arch_override": "x86_64"}
        try:
            _task_id = self.hub.build(src=source, target=release, opts=opts)
        except koji.ActionNotAllowed as exception:
            os.chdir(current_dir)
            raise exception from None
        except Exception as exception:
            logger.error(str(exception))
            os.chdir(current_dir)
            raise Exception("Failed building scratch build") from None

        os.chdir(current_dir)
        return _task_id



    def wait_task_complete(self, task_id):
        """
        Wait until koji finsihes building task
        """
        while True:
            with redirect_stdout(logger):
                watch_tasks(self.hub, [task_id], poll_interval=10)
            taskinfo = self.hub.getTaskInfo(task_id)
            state = taskinfo['state']

            # /usr/lib/python3.8/site-packages/koji_cli/lib.py
            if state == koji.TASK_STATES['CLOSED']:
                logger.info("task completed successfully")
                return True
            if state == koji.TASK_STATES['FAILED']:
                logger.info("task failed")
                return False
            if state == koji.TASK_STATES['CANCELED']:
                logger.info("was canceled")
                return False
            # shouldn't happen
            logger.info("task has not completed yet")


def main():
    """
    Prepare a qcow2 image for specific Fedora Release
    """
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--repo", "-r", dest="repo", required=True,
                        help="directory with repo spec file")
    parser.add_argument("--dist-ver", "-d", dest="dist_ver", required=True, help="ex: f33")
    parser.add_argument("--release", dest="release", help="Release. Ex: f32 or rawhide")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
    args = parser.parse_args()


    dist_ver = args.dist_ver.lower()
    release = args.release.lower()

    configure_logging(verbose=args.verbose, output_file=output_log)

    mykoji = Koji()
    global task_id
    task_id = mykoji.create_build(args.repo, dist_ver, release)
    if not mykoji.wait_task_complete(task_id):
        raise Exception("There was some problem creating scratch build")

    result = {"status": 0, "task_id": task_id, "log": output_log}
    with open(result_file, "w") as _file:
        json.dump(result, _file, indent=4, sort_keys=True, separators=(',', ': '))


if __name__ == "__main__":
    try:
        main()
    except Exception as exception:
        traceback.print_exc()
        logger.error(str(exception))
        result = {"status": 1, "task_id": task_id, "error_reason": str(exception), "log": output_log}
        with open(result_file, "w") as _file:
            json.dump(result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)

    sys.exit(0)
