
"""
Checkout specific git repo, branch
"""
import argparse
import logging
import os
import sys
import re
import traceback
import glob
import subprocess
import json
import time
import shutil

from git import Repo

# pylint: disable=logging-format-interpolation


class Runner():
    """
    Class to clone git repo and checkout branch.
    Also returns a list of valid test playbooks
    """
    logger = None
    logs = None
    result_file = None
    output_log = None
    result = {}

    git_url = None
    repo = None
    branch = None
    namespace = None
    pr = None

    def configure_logging(self, verbose=False):
        """Configure logging
        If verbose is set, set debug level for the default console logger
        If output_file is set, the logs are also saved on file
        """

        self.logger = logging.getLogger(__name__)

        logger_lvl = logging.INFO

        if verbose:
            logger_lvl = logging.DEBUG

        self.logger.setLevel(logger_lvl)

        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)

        formatter = logging.Formatter("%(levelname)s: %(message)s")
        ch.setFormatter(formatter)
        self.logger.addHandler(ch)

        if self.output_log:
            if os.path.isfile(self.output_log):
                os.remove(self.output_log)
            output_fh = logging.FileHandler(self.output_log)
            output_fh.setLevel(logger_lvl)
            output_fh.setFormatter(formatter)
            self.logger.addHandler(output_fh)

    def clone_repo(self):
        """
        Clone a git repo
        Apply patch from PR to git branch (optional)
        """

        repo_url = "{0}/{1}/{2}.git".format(self.git_url, self.namespace, self.repo)
        try:
            shutil.rmtree(self.repo)
        except FileNotFoundError:
            pass
        self.logger.info("Clonning {}".format(repo_url))

        max_retry = 5
        attempt = 1
        while True:
            try:
                git_repo = Repo.clone_from(repo_url, "./{}".format(self.repo))
            except Exception as exception:
                attempt += 1
                if attempt > max_retry:
                    self.logger.error(exception)
                    raise Exception("Couldn't clone {}".format(repo_url))
                time.sleep(10)
                self.logger.info("Retrying to clone {}. Attempt {}/{}".format(repo_url, attempt, max_retry))
                continue
            break

        self.logger.info("Checkout {}".format(self.branch))
        git_repo.git.checkout(self.branch)

        if not self.pr:
            return

        current_dir = os.getcwd()
        os.chdir(self.repo)

        self.logger.info("Fetching PR {}".format(self.pr))
        cmd = "git fetch -fu origin refs/pull/{}/head:pr".format(self.pr)
        self.logger.debug("Running {}".format(cmd))
        attempt = 1
        while True:
            try:
                subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            except subprocess.CalledProcessError as exception:
                attempt += 1
                if attempt > max_retry:
                    self.logger.error(str(exception))
                    if exception.stderr:
                        self.logger.debug(exception.stderr)
                    if exception.stdout:
                        self.logger.debug(exception.stdout)
                    raise Exception("Couldn't fetch PR {}".format(self.pr)) from None
                self.logger.info("Retrying to fetch PR {}. Attempt {}/{}".format(self.pr, attempt, max_retry))
                continue
            break

        self.logger.info("Merging PR {} to {}".format(self.pr, self.branch))
        cmd = ["git", "-c", "user.name=Fedora CI", "-c", "user.email=ci@lists.fedoraproject.org",
               "merge", "pr", "-m", "Fedora CI pipeline"]
        self.logger.debug("Running {}".format(" ".join(cmd)))
        try:
            subprocess.run(cmd, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            self.logger.error(str(exception))
            if exception.stderr:
                self.logger.error(exception.stderr)
            if exception.stdout:
                self.logger.error(exception.stdout)
            raise Exception("Couldn't merge {}".format(self.pr)) from None

        os.chdir(current_dir)

    def _has_classic_tag(self, playbook):
        self.logger.debug("Checking if playbook {} has classic tag".format(playbook))
        cmd = "ansible-playbook --tags classic --list-tags {}".format(playbook)
        output = subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        for line in output.stdout.split("\n"):
            if re.match(r"\s+TASK TAGS: \[.*\bclassic\b.*\]", line):
                self.logger.debug("playbook {} has classic tag".format(playbook))
                return True
            if re.match(r"\s+TASK TAGS: \[.*\balways\b.*\]", line):
                self.logger.debug("playbook {} has always tag".format(playbook))
                return True
        self.logger.debug("playbook {} has NOT classic tag".format(playbook))
        return False

    def get_test_playbooks(self):
        """
        Check if repo has tests/tests*.yml files
        Return the list of test playbooks
        """
        self.logger.info("Checking if {} has test playbooks".format(self.repo))
        current_dir = os.getcwd()
        tests_dir = "{}/tests".format(self.repo)
        if self.namespace == "tests":
            tests_dir = "./"
        try:
            os.chdir(tests_dir)
        except FileNotFoundError:
            os.chdir(current_dir)
            self.logger.debug("There is no tests directory")
            return []

        tmp_playbooks = glob.glob("tests*.yml")
        playbooks = []
        os.chdir(current_dir)
        for play in tmp_playbooks:
            if self._has_classic_tag("{}/{}".format(tests_dir, play)):
                playbooks.append(play)

        self.logger.debug("Test playbooks are: {}".format(",".join(playbooks)))
        return playbooks

    def main(self):
        """
        Checkout git repository
        Check if repo has test playbooks
        """
        parser = argparse.ArgumentParser(description='')
        parser.add_argument("--git-url", "-g", dest="git_url", required=False,
                            default="https://src.fedoraproject.org", help="Ex: https://src.fedoraproject.org")
        parser.add_argument("--repo", "-r", dest="repo", required=True,
                            help="Pagure repository name")
        parser.add_argument("--branch", "-b", dest="branch", required=False,
                            default="master", help="Pagure branch")
        parser.add_argument("--namespace", "-n", dest="namespace", required=False,
                            default="rpms", help="Pagure namespace")
        parser.add_argument("--logs", "-l", dest="logs", default="./",
                            help="Path where logs will be stored")
        parser.add_argument("--pr", "-p", dest="pr", help="Pagure pull request number")
        parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
        args = parser.parse_args()

        if not os.path.isdir(args.logs):
            os.makedirs(args.logs)

        self.logs = os.path.abspath(args.logs)
        self.result_file = "{}/checkout-repo.json".format(self.logs)
        self.output_log = "{}/checkout-repo.log".format(self.logs)

        self.configure_logging(verbose=args.verbose)

        self.git_url = args.git_url
        self.repo = args.repo
        self.branch = args.branch
        self.namespace = args.namespace
        self.pr = args.pr

        self.clone_repo()

        playbooks = self.get_test_playbooks()

        self.result = {"status": 0, "test_playbooks": playbooks, "log": self.output_log}
        with open(self.result_file, "w") as _file:
            json.dump(self.result, _file, indent=4, sort_keys=True, separators=(',', ': '))


if __name__ == "__main__":
    runner = Runner()
    try:
        runner.main()
    except Exception as exception:
        traceback.print_exc()
        runner.logger.error(str(exception))
        runner.result = {"status": 1, "test_playbooks": None, "error_reason": str(exception), "log": runner.output_log}
        with open(runner.result_file, "w") as _file:
            json.dump(runner.result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)

    sys.exit(0)
