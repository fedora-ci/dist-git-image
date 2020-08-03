
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
import shutil

from contextlib import redirect_stdout
import requests

import koji
from koji_cli.lib import watch_tasks
from git import Repo

global logger
global task_id
logger = None
task_id = None

result_file = "build-pr-result.json"
output_log = "build-pr.log"

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


    def build_pr(self, pagure_url, repo, branch, pr):
        """
        Apply patch to git branch and create an scratch build
        """

        fed_release = branch
        fed_dist = branch
        if branch == "master":
            dist_number = _rawhide_dist_number()
            if not dist_number:
                raise Exception("Coudln't figure out fedora release number for master branch")
            fed_dist = "f{}".format(dist_number)
            fed_release = "rawhide"

        git_url = "{0}/rpms/{1}.git".format(pagure_url, repo)
        try:
            shutil.rmtree(repo)
        except FileNotFoundError:
            pass
        logger.info("Clonning {}".format(git_url))
        git_repo = Repo.clone_from(git_url, "./{}".format(repo))

        logger.info("Checkout {}".format(branch))
        git_repo.git.checkout(branch)


        current_dir = os.getcwd()
        os.chdir(repo)

        logger.info("Fetching PR {}".format(pr))
        cmd = "git fetch -fu origin refs/pull/{}/head:pr".format(pr)
        logger.debug("Running {}".format(cmd))
        try:
            subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            logger.error(str(exception))
            if exception.stderr:
                logger.debug(exception.stderr)
            if exception.stdout:
                logger.debug(exception.stdout)
            raise Exception("Couldn't fetch PR {}".format(pr)) from None

        logger.info("Merging PR {} to {}".format(pr, branch))
        cmd = ["git", "-c", "user.name=Fedora CI", "-c", "user.email=ci@lists.fedoraproject.org", "merge", "pr", "-m", "Fedora CI pipeline"]
        logger.debug("Running {}".format(" ".join(cmd)))
        try:
            subprocess.run(cmd, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            logger.error(str(exception))
            if exception.stderr:
                logger.error(exception.stderr)
            if exception.stdout:
                logger.error(exception.stdout)
            raise Exception("Couldn't merge {}".format(pr)) from None

        logger.info("Bulding src...")
        cmd = "fedpkg --release {} srpm".format(fed_dist)
        logger.debug("Running {}".format(cmd))
        try:
            subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            logger.error(str(exception))
            if exception.stderr:
                logger.debug(exception.stderr)
            if exception.stdout:
                logger.debug(exception.stdout)
            raise Exception("Couldn't build src {}".format(pr)) from None

        srpms = glob.glob("*.src.rpm")
        if not srpms:
            raise Exception("Couldn't find src.rpm file")

        logger.info("Building scratch build for {} {}".format(repo, pr))
        opts = {"scratch": True, "arch-override": "x86_64"}
        try:
            task_id = self.hub.build(src=srpms[0], target=fed_release, opts=opts)
        except koji.ActionNotAllowed as exception:
            raise exception from None
        except Exception as exception:
            logger.error(str(exception))
            raise Exception("Failed building scratch build") from None

        os.chdir(current_dir)
        return task_id



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
    parser.add_argument("--git-url", "-g", dest="git_url", required=False,
                        default="https://src.fedoraproject.org", help="Ex: https://src.fedoraproject.org")
    parser.add_argument("--repo", "-r", dest="repo", required=True,
                        help="Pagure repository name")
    parser.add_argument("--branch", "-b", dest="branch", required=True, help="Pagure branch")
    parser.add_argument("--pr", "-p", dest="pr", help="Pagure pull request number")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose, output_file=output_log)

    mykoji = Koji()
    global task_id
    task_id = mykoji.build_pr(args.git_url, args.repo, args.branch, args.pr)
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
        result = {"status": 1, "task_id": None, "error_reason": str(exception), "log": output_log}
        with open(result_file, "w") as _file:
            json.dump(result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)

    sys.exit(0)
