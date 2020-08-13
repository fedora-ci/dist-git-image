
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

this = sys.modules[__name__]

this.task_id = None
this.logger = None

this.result_file = None
this.output_log = None

# pylint: disable=logging-format-interpolation


requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)  # pylint: disable=no-member


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


class Koji():
    """
    Class to handle operations on koji
    """
    def __init__(self):
        self.hub = koji.ClientSession("https://koji.fedoraproject.org/kojihub")
        keytab = os.getenv('KOJI_KEYTAB')
        if keytab:
            this.logger.debug("Authenticating to keytab")
            try:
                self.hub.gssapi_login(keytab=keytab)
            except Exception as exception:
                this.logger.err(str(exception))
                raise Exception("Couldn't authenticate using keytab")

    def create_build(self, repo, dist_ver, release):
        """
        Build scratch build from specific repo
        """
        this.logger.info("Bulding src...")
        current_dir = os.getcwd()
        os.chdir(repo)
        cmd = "fedpkg --release {} srpm".format(dist_ver)
        this.logger.debug("Running {}".format(cmd))
        try:
            subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            this.logger.error(str(exception))
            if exception.stderr:
                this.logger.debug(exception.stderr)
            if exception.stdout:
                this.logger.debug(exception.stdout)
            os.chdir(current_dir)
            raise Exception("Couldn't create src.rpm") from None

        srpms = glob.glob("*.src.rpm")
        if not srpms:
            os.chdir(current_dir)
            raise Exception("Couldn't find src.rpm file")

        source = srpms[0]
        this.logger.debug("Going to upload {}".format(source))
        serverdir = unique_path('cli-build')
        # callback = _progress_callback
        callback = None
        this.logger.debug("uploading {} to {}".format(source, serverdir))
        max_retry = 5
        attempt = 1
        while True:
            try:
                self.hub.uploadWrapper(source, serverdir, callback=callback)
            except Exception as exception:
                attempt += 1
                if attempt > max_retry:
                    this.logger.error(str(exception))
                    os.chdir(current_dir)
                    raise Exception("Failed uploading {}".format(source)) from None
                time.sleep(10)
                this.logger.info("Retrying to upload {}. Attempt {}/{}".format(source, attempt, max_retry))
                continue
            break
        source = "%s/%s" % (serverdir, os.path.basename(source))

        this.logger.info("Building scratch build for {}".format(source))
        opts = {"scratch": True, "arch_override": "x86_64"}
        try:
            _task_id = self.hub.build(src=source, target=release, opts=opts)
        except koji.ActionNotAllowed as exception:
            os.chdir(current_dir)
            raise exception from None
        except Exception as exception:
            this.logger.error(str(exception))
            os.chdir(current_dir)
            raise Exception("Failed building scratch build") from None

        os.chdir(current_dir)
        return _task_id

    def wait_task_complete(self, task_id):
        """
        Wait until koji finsihes building task
        """
        while True:
            with redirect_stdout(this.logger):
                watch_tasks(self.hub, [task_id], poll_interval=10)
            taskinfo = self.hub.getTaskInfo(task_id)
            state = taskinfo['state']

            # /usr/lib/python3.8/site-packages/koji_cli/lib.py
            if state == koji.TASK_STATES['CLOSED']:
                this.logger.info("task completed successfully")
                return True
            if state == koji.TASK_STATES['FAILED']:
                this.logger.info("task failed")
                return False
            if state == koji.TASK_STATES['CANCELED']:
                this.logger.info("was canceled")
                return False
            # shouldn't happen
            this.logger.info("task has not completed yet")


def main():
    """
    Prepare a qcow2 image for specific Fedora Release
    """
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--repo", "-r", dest="repo", required=True,
                        help="directory with repo spec file")
    parser.add_argument("--release", dest="release", help="Release. Ex: f32 or rawhide")
    parser.add_argument("--logs", "-l", dest="logs", default="./",
                        help="Path where logs will be stored")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(args.logs):
        os.makedirs(args.logs)

    logs = os.path.abspath(args.logs)

    this.result_file = "{}/create-build.json".format(logs)
    this.output_log = "{}/create-build.log".format(logs)

    release = args.release.lower()

    if release == "rawhide":
        dist_ver = "master"

    configure_logging(verbose=args.verbose, output_file=this.output_log)

    mykoji = Koji()
    this.task_id = mykoji.create_build(args.repo, dist_ver, release)
    if not mykoji.wait_task_complete(this.task_id):
        raise Exception("There was some problem creating scratch build")

    this.result = {"status": 0, "task_id": this.task_id, "log": this.output_log}
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
