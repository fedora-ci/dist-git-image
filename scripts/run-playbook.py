
"""
Checkout specific git repo, branch
"""
import argparse
import logging
import os
import sys
import traceback
import multiprocessing
import time
import subprocess
import json
import yaml

from ansible import context
from ansible.cli import CLI
from ansible.module_utils.common.collections import ImmutableDict
from ansible.executor import playbook_executor
from ansible.parsing.dataloader import DataLoader
from ansible.inventory.manager import InventoryManager
from ansible.vars.manager import VariableManager
from ansible.utils.display import Display

# pylint: disable=logging-format-interpolation


def _destroy_vm():
    kill_cmd = "killall /usr/bin/qemu-system-x86_64"
    try:
        subprocess.run(kill_cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError:
        pass


def _run_pbex(pbex, return_dict):
    return_dict["exit_code"] = pbex.run()


class Runner():
    """
    Class to run ansible playbook on a VM
    """

    test_artifacts = None
    result_file = None
    output_log = None
    inventory_file = None
    result = {}

    # https://serversforhackers.com/c/running-ansible-2-programmatically
    display = Display()

    def __init__(self):
        self.logger = None

    def configure_logging(self, verbose=False, output_file=None):
        """Configure logging
        If verbose is set, set debug level for the default console logger
        If output_file is set, the logs are also saved on file
        Return logger object.
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

        if output_file:
            if os.path.isfile(output_file):
                os.remove(output_file)
            output_fh = logging.FileHandler(output_file)
            output_fh.setLevel(logger_lvl)
        output_fh.setFormatter(formatter)
        self.logger.addHandler(output_fh)

    def provision(self, image):
        """
        Use ansible to provision a dynamic invnetory
        """

        self.logger.info("Provisioning {}".format(image))
        env = os.environ.copy()
        env['TEST_DEBUG'] = '1'
        env['TEST_SUBJECTS'] = image
        env['TEST_ARTIFACTS'] = self.test_artifacts
        ansible_inventory = "/usr/share/ansible/inventory/standard-inventory-qcow2"
        if os.path.isfile("inventory"):
            ansible_inventory = "inventory"

        # guest and qemu logs are created by STR based on self.test_artifacts
        base_image = os.path.basename(image)
        self.result["guest_log"] = "{}/{}.guest.log".format(self.test_artifacts, base_image)
        self.result["qemu_log"] = "{}/{}.qemu.log".format(self.test_artifacts, base_image)

        cmd = "ansible-inventory --inventory={} --list --yaml".format(ansible_inventory)
        self.logger.debug("Running TEST_DEBUG={} TEST_SUBJECTS={} {}".format(env['TEST_DEBUG'], env['TEST_SUBJECTS'], cmd))
        max_retry = 5
        attempt = 1
        while True:
            # make sure there is no VM running
            _destroy_vm()
            try:
                inventory = subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=env)
            except subprocess.CalledProcessError as exception:
                attempt += 1
                if attempt > max_retry:
                    self.logger.error(str(exception))
                    if exception.stderr:
                        self.logger.debug(exception.stderr)
                    if exception.stdout:
                        self.logger.debug(exception.stdout)
                    raise Exception("Couldn't provision {}".format(image)) from None
                self.logger.info("Retrying to provision {}. Attempt {}/{}".format(image, attempt, max_retry))
                continue
            # make sure the inventory is valid
            try:
                parsed_inventory = yaml.safe_load(inventory.stdout)
                localhost = parsed_inventory["all"]["children"]["localhost"]
            except Exception as exception:
                attempt += 1
                if attempt > max_retry:
                    self.logger.error(str(exception))
                    self.logger.error(inventory.stdout)
                    raise Exception("Invalid inventory") from None
                self.logger.info("Invalid inventory. Retrying to provision {}. Attempt {}/{} ".format(image, attempt, max_retry))
                continue
            break

        with open(self.inventory_file, "w") as _file:
            yaml.dump(parsed_inventory, _file)
        # wait some time so all info from inventory are printed
        time.sleep(5)

        self.logger.info("VM is up")
        self.logger.debug("Ansible inventory saved on {}".format(self.inventory_file))

    def run_playbook(self, playbook, extra_vars=None, check_result=False):
        """
        Run an ansible playbook
        """
        # https://stackoverflow.com/questions/27590039/running-ansible-playbook-using-python-api

        loader = DataLoader()

        if extra_vars:
            extra_vars = set(extra_vars)
        else:
            extra_vars = {}

        context.CLIARGS = ImmutableDict(tags={"classic"}, listtags=False, listtasks=False,
                                        listhosts=False, syntax=False, connection='ssh',
                                        module_path=None, forks=100, remote_user='xxx',
                                        private_key_file=None, ssh_common_args=None,
                                        ssh_extra_args=None, sftp_extra_args=None, extra_vars=extra_vars,
                                        scp_extra_args=None, become=True, become_method='sudo',
                                        become_user='root', verbosity=True, check=False, start_at_task=None)

        inventory = InventoryManager(loader=loader, sources=(self.inventory_file,))

        variable_manager = VariableManager(loader=loader, inventory=inventory, version_info=CLI.version_info(gitinfo=False))

        passwords = {}

        os.environ['TEST_ARTIFACTS'] = os.path.abspath(self.test_artifacts)
        self.logger.debug("TEST_ARTIFACTS = {}".format(os.environ['TEST_ARTIFACTS']))
        # TODO: save execution output to file
        pbex = playbook_executor.PlaybookExecutor(playbooks=[playbook], inventory=inventory, variable_manager=variable_manager, loader=loader, passwords=passwords)
        pbex._tqm._stdout_callback = "yaml"  # pylint: disable=protected-access

        self.logger.info("Running playbook {}".format(playbook))
        # https://stackoverflow.com/questions/10415028/how-can-i-recover-the-return-value-of-a-function-passed-to-multiprocessing-proce
        manager = multiprocessing.Manager()
        return_dict = manager.dict()
        run_process = multiprocessing.Process(target=_run_pbex, args=(pbex, return_dict))
        run_process.start()
        # run playbook with timeout of 4hrs
        run_process.join(4*60*60)
        if run_process.is_alive():
            self.logger.error("Playbook has been running for too long. Aborting it.")
            run_process.terminate()
            run_process.join()
            exit_code = 1
        else:
            if "exit_code" in return_dict:
                exit_code = return_dict["exit_code"]
            else:
                exit_code = 1
        self.logger.debug("Playbook {} finished with {}".format(playbook, exit_code))

        if check_result:
            # check if result matches: https://docs.fedoraproject.org/en-US/ci/standard-test-interface/
            if not os.path.isfile("{}/test.log".format(self.test_artifacts)):
                self.logger.error("Playbook finished without creating test.log")
                exit_code = 1

            results_yml_file = "{}/results.yml".format(self.test_artifacts)
            if not os.path.isfile(results_yml_file):
                self.logger.debug("playbook didn't create results.yml, creating one...")
                # playbook didn't create results.yml, so create one
                test_result = {}
                test_result['test'] = os.path.splitext(os.path.basename(playbook))[0]
                test_result['logs'] = [self.test_artifacts]
                if exit_code == 0:
                    test_result['result'] = "pass"
                else:
                    test_result['result'] = "fail"
                result = {}
                result['results'] = [test_result]
                with open(results_yml_file, "w") as _file:
                    yaml.safe_dump(result, _file)

            self.logger.debug("parsing results.yml")
            with open(results_yml_file) as _file:
                parsed_yaml = yaml.load(_file, Loader=yaml.FullLoader)
            for result in parsed_yaml["results"]:
                if result['result'] != "pass":
                    self.logger.debug("{} has result {}, setting whole playbook as failed".format(result['test'], result['result']))
                    exit_code = 1

        return exit_code

    def main(self):
        """
        Provision a VM from qcow2 and safe ansible inventory to a file
        """
        parser = argparse.ArgumentParser(description='')
        parser.add_argument("--image", "-i", dest="image", required=True,
                            help="Path to qcow2 image")
        parser.add_argument("--artifacts", "-a", dest="artifacts", required=True,
                            help="Path where logs and tests results will be stored")
        parser.add_argument("--extra-vars", "-e", dest="extra_vars", required=False,
                            action="append", help="Extra ansible variables. 'key=value' format")
        parser.add_argument("--playbook", "-p", dest="playbook", required=True,
                            help="Playbook to run")
        parser.add_argument("--no-check-result", dest="check_result", action="store_false")
        parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
        args = parser.parse_args()

        if not os.path.isdir(args.artifacts):
            os.makedirs(args.artifacts)

        self.test_artifacts = os.path.abspath(args.artifacts)
        self.result_file = "{}/run-playbook.json".format(self.test_artifacts)
        self.output_log = "{}/run-playbook.log".format(self.test_artifacts)
        self.inventory_file = "{}/pipeline_inventory.yaml".format(self.test_artifacts)

        self.result = {"status": 1, "inventory": self.inventory_file,
                       "artifacts": self.test_artifacts, "log": self.output_log}

        self.configure_logging(verbose=args.verbose, output_file=self.output_log)

        verbosity = 1
        self.display.verbosity = verbosity
        playbook_executor.verbosity = verbosity

        self.provision(args.image)

        # back up previous results.yml
        results_file = "{}/results.yml".format(self.test_artifacts)
        results_bak_file = "{}/results.yml.bak".format(self.test_artifacts)
        if os.path.isfile(results_file):
            self.logger.debug("backing up {}".format(results_file))
            os.rename(results_file, results_bak_file)

        exit_code = self.run_playbook(args.playbook, args.extra_vars, args.check_result)

        # add new results to old results and save them as results.yml
        if (os.path.isfile(results_file) and os.path.isfile(results_bak_file)):
            with open(results_file) as _file:
                parsed_yaml = yaml.load(_file, Loader=yaml.FullLoader)
            with open(results_bak_file) as _file:
                parsed_bak_yaml = yaml.load(_file, Loader=yaml.FullLoader)
            # add new results to backed up ones
            self.logger.debug("merging results")
            for result in parsed_bak_yaml["results"]:
                parsed_yaml["results"].append(result)

            with open(results_file, "w") as _file:
                yaml.safe_dump(parsed_yaml, _file)
            os.remove(results_bak_file)

        if exit_code != 0:
            self.display.verbosity = 0
            # make sure even if playbooks doesn't fetch logs from VM the artficats is synced
            self.run_playbook("/tmp/sync-artifacts.yml")

        self.result["status"] = exit_code
        with open(self.result_file, "w") as _file:
            json.dump(self.result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(exit_code)


if __name__ == "__main__":
    runner = Runner()
    try:
        runner.main()
    except Exception as exception:
        traceback.print_exc()
        runner.logger.error(str(exception))
        runner.result["status"] = 1
        runner.result["error_reason"] = str(exception)
        with open(runner.result_file, "w") as _file:
            json.dump(runner.result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)

    sys.exit(0)
