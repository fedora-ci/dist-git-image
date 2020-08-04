
"""
Checkout specific git repo, branch
"""
import argparse
import logging
import os
import sys
import traceback
import glob
import subprocess
import json
import time
import shutil

from git import Repo

global logger
logger = None

result_file = "{}/checkout-repo-result.json".format(os.getcwd())
output_log = "{}/checkout-repo.log".format(os.getcwd())

#pylint: disable=logging-format-interpolation

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


def clone_repo(pagure_url, repo, branch, namespace, pr=None):
    """
    Clone a git repo
    Apply patch from PR to git branch (optional)
    """

    git_url = "{0}/{1}/{2}.git".format(pagure_url, namespace, repo)
    try:
        shutil.rmtree(repo)
    except FileNotFoundError:
        pass
    logger.info("Clonning {}".format(git_url))

    max_retry = 5
    attempt = 1
    while True:
        try:
            git_repo = Repo.clone_from(git_url, "./{}".format(repo))
        except Exception as exception:
            attempt += 1
            if attempt > max_retry:
                logger.error(exception)
                raise Exception("Couldn't clone {}".format(git_url))
            time.sleep(10)
            logger.info("Retrying to clone {}. Attempt {}/{}".format(git_url, attempt, max_retry))
            continue
        break


    logger.info("Checkout {}".format(branch))
    git_repo.git.checkout(branch)

    if not pr:
        return

    current_dir = os.getcwd()
    os.chdir(repo)

    logger.info("Fetching PR {}".format(pr))
    cmd = "git fetch -fu origin refs/pull/{}/head:pr".format(pr)
    logger.debug("Running {}".format(cmd))
    attempt = 1
    while True:
        try:
            subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            attempt += 1
            if attempt > max_retry:
                logger.error(str(exception))
                if exception.stderr:
                    logger.debug(exception.stderr)
                if exception.stdout:
                    logger.debug(exception.stdout)
                raise Exception("Couldn't fetch PR {}".format(pr)) from None
            logger.info("Retrying to fetch PR {}. Attempt {}/{}".format(pr, attempt, max_retry))
            continue
        break

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


    os.chdir(current_dir)


def get_test_playbooks(repo, namespace):
    """
    Check if repo has tests/tests*.yml files
    Return the list of test playbooks
    """
    logger.info("Checking if {} has test playbooks".format(repo))
    current_dir = os.getcwd()
    tests_dir = "{}/tests".format(repo)
    if namespace == "tests":
        tests_dir = "./"
    try:
        os.chdir(tests_dir)
    except FileNotFoundError:
        os.chdir(current_dir)
        logger.debug("There is no tests directory")
        return []

    playbooks = glob.glob("tests*.yml")
    os.chdir(current_dir)
    logger.debug("Test playbooks are: {}".format(",".join(playbooks)))

    return playbooks


def main():
    """
    Checkout git repository
    Check if repo has test playbooks
    """
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--git-url", "-g", dest="git_url", required=False,
                        default="https://src.fedoraproject.org", help="Ex: https://src.fedoraproject.org")
    parser.add_argument("--repo", "-r", dest="repo", required=True,
                        help="Pagure repository name")
    parser.add_argument("--branch", "-b", dest="branch", required=True, help="Pagure branch")
    parser.add_argument("--namespace", "-n", dest="namespace", required=True, help="Pagure namespace")
    parser.add_argument("--pr", "-p", dest="pr", help="Pagure pull request number")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose, output_file=output_log)

    clone_repo(args.git_url, args.repo, args.branch, args.namespace, args.pr)

    playbooks = get_test_playbooks(args.repo, args.namespace)

    result = {"status": 0, "test_playbooks": playbooks, "log": output_log}
    with open(result_file, "w") as _file:
        json.dump(result, _file, indent=4, sort_keys=True, separators=(',', ': '))


if __name__ == "__main__":
    try:
        main()
    except Exception as exception:
        traceback.print_exc()
        logger.error(str(exception))
        result = {"status": 1, "test_playbooks": None, "error_reason": str(exception), "log": output_log}
        with open(result_file, "w") as _file:
            json.dump(result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)

    sys.exit(0)
