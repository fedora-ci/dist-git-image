
"""
Checkout specific git repo, branch
"""
import argparse
import logging
import os
import sys
import traceback
import subprocess
import json
import yaml

global logger
logger = None

result_file = "provision-result.json"
output_log = "provision-repo.log"

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


def provision(image, inventory_file):
    """
    Use ansible to provision a dynamic invnetory
    """

    logger.info("Provisioning {}".format(image))
    env = os.environ.copy()
    env['TEST_DEBUG'] = '1'
    env['TEST_SUBJECTS'] = image
    ansible_inventory = "/usr/share/ansible/inventory/standard-inventory-qcow2"
    if os.path.isfile("inventory"):
        ansible_inventory = "inventory"
    cmd = "ansible-inventory --inventory={} --list --yaml".format(ansible_inventory)
    kill_cmd = "killall /usr/bin/qemu-system-x86_64"
    logger.debug("Running TEST_DEBUG={} TEST_SUBJECTS={} {}".format(env['TEST_DEBUG'], env['TEST_SUBJECTS'], cmd))
    max_retry = 5
    attempt = 1
    while True:
        try:
            subprocess.run(kill_cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as exception:
            pass
        try:
            inventory = subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=env)
        except subprocess.CalledProcessError as exception:
            attempt += 1
            if attempt > max_retry:
                logger.error(str(exception))
                if exception.stderr:
                    logger.debug(exception.stderr)
                if exception.stdout:
                    logger.debug(exception.stdout)
                raise Exception("Couldn't provision".format(image)) from None
            logger.info("Retrying to provision {}. Attempt {}/{}".format(image, attempt, max_retry))
            continue
        # make sure the inventory is valid
        try:
            parsed_inventory = yaml.safe_load(inventory.stdout)
            localhost = parsed_inventory["all"]["children"]["localhost"]
        except Exception as exception:
            attempt += 1
            if attempt > max_retry:
                logger.error(str(exception))
                raise Exception("Invalid inventory".format(image)) from None
            logger.info("Invalid inventory. Retrying to provision {}. Attempt {}/{} ".format(image, attempt, max_retry))
            continue
        break

    with open(inventory_file, "w") as _file:
        yaml.dump(parsed_inventory, _file)


def main():
    """
    Provision a VM from qcow2 and safe ansible inventory to a file
    """
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--image", "-i", dest="image", required=True,
                        help="Path to qcow2 image")
    parser.add_argument("--output", dest="output", required=True,
                        help="File to save inventory file")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose, output_file=output_log)

    provision(args.image, args.output)

    result = {"status": 0, "inventory": args.output, "log": output_log}
    with open(result_file, "w") as _file:
        json.dump(result, _file, indent=4, sort_keys=True, separators=(',', ': '))


if __name__ == "__main__":
    try:
        main()
    except Exception as exception:
        traceback.print_exc()
        logger.error(str(exception))
        result = {"status": 1, "inventory": None, "error_reason": str(exception), "log": output_log}
        with open(result_file, "w") as _file:
            json.dump(result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)

    sys.exit(0)
