
"""
Prepare a qcow2 image to be used by Fedora dist-git pipeline
"""
import argparse
import logging
import os
import sys
import traceback
import re
import subprocess
import json
import time
import requests
import koji
import guestfs

this = sys.modules[__name__]

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


def download_qcow2(release):
    """
    Based on Fedora release download the correct qcow2 image
    """
    url = None
    match = re.match(r"f(\d+)", release)
    base_url = "https://jenkins-continuous-infra.apps.ci.centos.org/job"
    if release == "rawhide":
        url = "{}/fedora-rawhide-image-test/lastSuccessfulBuild/artifact/Fedora-Rawhide.qcow2".format(base_url)
    elif match:
        url = "{0}/fedora-f{1}-image-test/lastSuccessfulBuild/artifact/Fedora-{1}.qcow2".format(base_url, match.group(1))
    else:
        raise Exception("Unsupported release {}".format(release))

    qcow2_file = "{}/{}".format(this.artifacts, url.split("/")[-1])
    if os.path.isfile(qcow2_file):
        this.logger.info("{} already exists, no need to download.".format(qcow2_file))
        return qcow2_file

    this.logger.info("Downloading {}".format(url))
    this.logger.debug("qcow2 will be saved to {}".format(qcow2_file))
    # NOTE: I couldn't find a python way to do this curl command using requests or urllib.request.urlretrieve
    # at least not one that would work as reliable
    curl_cmd = "curl --fail --connect-timeout 5 --retry 10 --retry-delay 0 --retry-max-time 60 -C - -L -k -O {}".format(url)
    this.logger.debug("Running {}".format(curl_cmd))
    try:
        result_run = subprocess.run(curl_cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=this.artifacts, check=True)
    except subprocess.CalledProcessError as exception:
        if exception.stderr:
            this.logger.info(exception.stderr)
        if exception.stdout:
            this.logger.info(exception.stdout)
        if os.path.isfile(qcow2_file):
            this.logger.debug("removing partial qcow2 {}".format(qcow2_file))
            os.remove(qcow2_file)
        raise Exception("Couldn't download qcow2") from None
    if result_run.stderr:
        this.logger.info(result_run.stderr)
    if result_run.stdout:
        this.logger.info(result_run.stdout)
    this.logger.debug("qcow2 is available on {}".format(qcow2_file))
    return qcow2_file


def verify_qcow2(image):
    """
    Run qemu-img check to make sure the qcow2 is valid
    """
    if not os.path.isfile(image):
        raise Exception("{} doesn't exist".format(image))

    this.logger.info("Verifying {}".format(image))
    try:
        cmd = ["qemu-img", "check", image]
        result_run = subprocess.run(cmd, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as exception:
        this.logger.error(str(exception))
        if exception.stderr:
            this.logger.info(exception.stderr)
        if exception.stdout:
            this.logger.info(exception.stdout)
        raise Exception("Couldn't verify qcow2 image") from None
    if result_run.stderr:
        this.logger.debug(result_run.stderr)
    if result_run.stdout:
        this.logger.debug(result_run.stdout)
    return True


def create_repo(repo):
    """
    Creates an rpm repository on provided path
    """
    this.logger.debug("Creating repo for {}".format(repo))
    cmd = "createrepo ."
    try:
        result_run = subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=repo, check=True)
    except subprocess.CalledProcessError as exception:
        this.logger.error(str(exception))
        if exception.stderr:
            this.logger.debug(exception.stderr)
        if exception.stdout:
            this.logger.debug(exception.stdout)
        raise Exception("Couldn't create repo")
    if result_run.stderr:
        this.logger.debug(result_run.stderr)
    if result_run.stdout:
        this.logger.debug(result_run.stdout)
    return True


class Koji():
    """
    Module to handle operations on koji
    """
    def __init__(self):
        self.hub = koji.ClientSession("https://koji.fedoraproject.org/kojihub")
        self.koji_params = os.getenv("KOJI_PARAMS", "")

    def download_task(self, task_id, task_repo):
        """
        Use koji to download the rpms for specific TaskID
        the RPMS will be saved to provided task_repo directory
        """
        taskinfo = self.hub.getTaskInfo(task_id, request=True)
        is_scratch = (taskinfo['request'][2] and
                      'scratch' in taskinfo['request'][2] and
                      taskinfo['request'][2]['scratch'])
        default_params = "--arch=x86_64 --arch=src --arch=noarch"
        if is_scratch:
            cmd = "koji {} download-task {} {}".format(self.koji_params, default_params, task_id)
        else:
            cmd = "koji {} download-build {} --debuginfo --task-id {}".format(self.koji_params, default_params, task_id)
        if os.path.isdir(task_repo):
            this.logger.info("{} already exists, assume rpms are already downloaded. Skipping...".format(task_repo))
            return True
        os.makedirs(task_repo)

        this.logger.info("Downloading rpms from {}".format(task_id))
        this.logger.debug("Running {}".format(cmd))
        try:
            result_run = subprocess.run(cmd.split(), universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        cwd=task_repo, check=True)
        except subprocess.CalledProcessError as exception:
            this.logger.error(str(exception))
            if exception.stderr:
                this.logger.debug(exception.stderr)
            if exception.stdout:
                this.logger.debug(exception.stdout)
            raise Exception("Couldn't download task rpms")
        if result_run.stderr:
            this.logger.debug(result_run.stderr)
        if result_run.stdout:
            this.logger.debug(result_run.stdout)
        return True


class Qcow2():
    """
    Module to edit a qcow2 image
    """
    def __init__(self):
        self.g = guestfs.GuestFS(python_return_dict=True)
        self.dest_base_repo_path = "/opt/task_repos"

    def mount_fs(self):
        """
        Tries to mount all the devices on VM
        """
        roots = self.g.inspect_os()
        if len(roots) == 0:
            raise Exception("Couldn't find devices to mount")
        for root in roots:
            mnt_points = self.g.inspect_get_mountpoints(root)
            for mnt_point in mnt_points:
                try:
                    self.g.mount(mnt_points[mnt_point], mnt_point)
                except RuntimeError as exception:
                    this.logger.error(str(exception))
                    raise Exception("Couldn't mount {} to {}".format(mnt_points[mnt_point], mnt_point))
        return True

    def _copy_task_repos(self, task_repos):
        """
        copy task repos to qcow2
        create a dnf repo file for each repo
        """
        if not task_repos:
            this.logger.debug("no task repos provided, skipping...")
            return True

        self.g.mkdir_p(self.dest_base_repo_path)
        this.logger.info("Copying task repos to qcow2")
        for repo in task_repos:
            try:
                self.g.copy_in(repo, self.dest_base_repo_path)
            except RuntimeError as exception:
                this.logger.error(str(exception))
                raise Exception("Couldn't copy {} to {}".format(repo, self.dest_base_repo_path))
            name = os.path.basename(repo)
            task_repo = "[test-{}]\n".format(name)
            task_repo += "name=test-{}\n".format(name)
            task_repo += "baseurl=file://{}/{}\n".format(self.dest_base_repo_path, name)
            task_repo += "priority=0\n"
            task_repo += "enabled=1\n"
            task_repo += "gpgcheck=0\n"

            test_repo = "{}/test-{}.repo".format(this.artifacts, name)
            with open(test_repo, "w") as _file:
                _file.write(task_repo)
            try:
                self.g.copy_in(test_repo, "/etc/yum.repos.d")
            except RuntimeError as exception:
                this.logger.error(str(exception))
                raise Exception("Couldn't copy {} to {}".format(test_repo, "/etc/yum.repos.d"))

        this.logger.debug("repo {} copied to {}".format(name, self.dest_base_repo_path))
        this.logger.debug("file {} copied to {}".format(test_repo, "/etc/yum.repos.d"))
        return True

    def _add_latest_repo(self, release):
        """
        Enable the repository with latest builds for this release
        """
        fedora_dist = release
        if release == "rawhide":
            dist_num = _rawhide_dist_number()
            if not dist_num:
                raise Exception("Couldn't discover the Fedora dist number of rawhide")
            fedora_dist = "f{}".format(dist_num)

        koji_latest_repo = "[koji-{0}-build]\n".format(fedora_dist)
        koji_latest_repo += "name=koji-{0}-build\n".format(fedora_dist)
        koji_latest_repo += "baseurl=https://kojipkgs.fedoraproject.org/repos/{0}-build/latest/x86_64/\n".format(fedora_dist)
        koji_latest_repo += "enabled=1\n"
        koji_latest_repo += "gpgcheck=0\n"

        koji_latest_repo_file = "{}/koji-latest.repo".format(this.artifacts)
        with open(koji_latest_repo_file, "w") as _file:
            _file.write(koji_latest_repo)
        try:
            self.g.copy_in(koji_latest_repo_file, "/etc/yum.repos.d/")
        except RuntimeError as exception:
            this.logger.error(str(exception))
            raise Exception("Couldn't copy {} to {}".format(koji_latest_repo_file, "/etc/yum.repos.d"))

        this.logger.debug("file {} copied to {}".format(koji_latest_repo_file, "/etc/yum.repos.d"))
        return True

    def _install_rpms(self, task_id):
        """
        Install rpms from task_id, skip rpms with conflicts
        """
        this.logger.info("Going to install rpms from {}".format(task_id))
        # Get a list of conflicts from packages already installed in the image
        cmd = "dnf repoquery -q --conflict `rpm -qa --qf '%{NAME} '`"
        this.logger.debug("Getting conflict of already installed packages")
        queried_conflicts = False
        for i in range(5):
            try:
                installed_conflict_caps = self.g.sh(cmd)
            except RuntimeError as exception:
                this.logger.error(str(exception))
                this.logger.warning("Failed to get conflict of installed packages: {}/5".format(i))
                continue
            queried_conflicts = True
            break
        if not queried_conflicts:
            raise Exception("Could not query conflict of installed packages")

        installed_conflict_caps = installed_conflict_caps.split("\n")
        installed_conflicts = []
        repo_query_param = "--disablerepo=* --enablerepo={0} --repofrompath={0},{1}/{0}".format(task_id, self.dest_base_repo_path)
        if installed_conflict_caps:
            # from the possible conflicts get a list of packages would cause conflict
            for conflict_cap in installed_conflict_caps:
                cmd = 'dnf repoquery -q --qf "%{{NAME}}" {} --whatprovides "{}"'.format(repo_query_param, conflict_cap)
                try:
                    pkg_conflict = self.g.sh(cmd)
                except RuntimeError as exception:
                    this.logger.error(str(exception))
                    raise Exception("Failed to get what packages from repo conflicts with installed packages")
                if pkg_conflict:
                    installed_conflicts.extend(pkg_conflict.split("\n"))

        # Get all packages provided by task repo
        cmd = 'dnf repoquery -q {} --all --qf="%{{ARCH}}:%{{NAME}}"'.format(repo_query_param)
        this.logger.debug("Querying rpms provided by {}".format(task_id))
        try:
            raw_pkgs = self.g.sh(cmd).split("\n")
        except RuntimeError as exception:
            this.logger.debug(raw_pkgs)
            this.logger.error(str(exception))
            raise Exception("Failed to get list of packages from task repo")
        pkgs = []
        for raw_pkg in raw_pkgs:
            # not interested on src packages
            if re.match(r"^src.*", raw_pkg):
                continue
            # remove  everything until ':' from name
            name = re.sub(".+:", "", raw_pkg)
            if name == "":
                continue
            # not interested on -debug related pakcages
            if re.match(r".*-debug(info|source)$", name):
                continue
            pkgs.append(name)
        pkgs = sorted(pkgs)
        if not pkgs:
            raise Exception("Couldn't find any package to install")
        # For each package in task repo, check if it will not conflict
        rpm_list = []
        for pkg in pkgs:
            found_conflict = False
            cmd = "dnf repoquery -q {} --conflict {}".format(repo_query_param, pkg)
            this.logger.debug("Querying what conflicts with {} from {}".format(pkg, task_id))
            conflict_caps = self.g.sh(cmd).split("\n")
            for conflict_cap in conflict_caps:
                if conflict_cap == "":
                    continue
                this.logger.debug("Checking if any package from {} provides conflict {}".format(task_id, conflict_cap))
                cmd = 'dnf repoquery -q --qf "%{{NAME}}" {} --whatprovides "{}"'.format(repo_query_param, conflict_cap)
                conflicts = self.g.sh(cmd).split("\n")
                for conflict in conflicts:
                    if conflict in rpm_list:
                        # pkg conflicts with a package already in the list to be installed
                        found_conflict = True
                        continue
            if found_conflict:
                this.logger.info("will not install {} as it conflicts with {}.".format(pkg, " ".join(conflict_caps)))
                continue
            if pkg in installed_conflicts:
                this.logger.info("will not install {} as it conflicts with installed package {}.".format(pkg, " ".join(installed_conflicts)))
                continue
            rpm_list.append(pkg)

        if not rpm_list:
            raise Exception("There is no suitable rpm to be installed")

        rpm_list_str = " ".join(rpm_list)
        cmd = "dnf install -y --best --allowerasing --nogpgcheck {}".format(rpm_list_str)
        try:
            output = self.g.sh(cmd)
        except RuntimeError as exception:
            this.logger.error(str(exception))
            raise Exception("Couldn't install {}".format(rpm_list_str)) from None
        this.logger.debug("Installing rpms using {}".format(cmd))
        this.logger.debug(cmd)
        this.logger.debug(output)
        return True

    def prepare_qcow2(self, image, release, task_repos, task_ids, install_rpms, sys_update):
        """
        Add latest rpms repo to qcow2
        Copy task repos to qcow2
        Update the OS if requested
        """
        self.g.add_drive_opts(image, format="qcow2", readonly=0)
        self.g.set_memsize(4096)
        self.g.set_network(True)
        this.logger.info("Going to prepare {}".format(image))
        self.g.launch()
        if not self.mount_fs():
            return False
        if release == "rawhide":
            # Don't check GPG key when testing on Rawhide
            try:
                self.g.sh("sed -i s/gpgcheck=.*/gpgcheck=0/ /etc/yum.repos.d/*.repo")
            except RuntimeError as exception:
                this.logger.error(str(exception))
                raise Exception("Couldn't disable gpgcheck") from None
        else:
            try:
                self.g.sh("dnf config-manager --set-enable updates-testing updates-testing-debuginfo")
            except RuntimeError as exception:
                this.logger.error(str(exception))
                raise Exception("Couldn't enable updates testing repos") from None

        if not self._add_latest_repo(release):
            raise Exception("Could not not add koji latest repo to image")

        if not self._copy_task_repos(task_repos):
            raise Exception("Could not copy all task repos to image")

        # if task_id is provided and it should install the rpms
        if task_ids and install_rpms:
            for task_id in task_ids:
                if not self._install_rpms(task_id):
                    raise Exception("Could not install rpms from task {}".format(task_id))

        if sys_update:
            this.logger.info("Going to update the system")
            try:
                output = self.g.sh("dnf upgrade -y")
            except RuntimeError as exception:
                this.logger.error(str(exception))
                raise Exception("Couldn't upgrade system") from None
            this.logger.info(output)

        se_config = self.g.sh("cat /etc/selinux/config")
        se_type = None
        for line in se_config.split("\n"):
            match = re.match(r'^SELINUXTYPE=(\S+)', line)
            if match:
                se_type = match.group(1)
        if not se_type:
            raise Exception("Could not parse SElinux policy type")

        this.logger.debug("Going to relabel SELinux contexts")
        try:
            self.g.selinux_relabel("/etc/selinux/" + se_type + "/contexts/files/file_contexts", "/")
        except RuntimeError as exception:
            this.logger.error(str(exception))
            raise Exception("Couldn't relabel selinux contexts") from None

        this.logger.info("{} is Ready".format(image))
        return True


def main():
    """
    Prepare a qcow2 image for specific Fedora Release
    """
    parser = argparse.ArgumentParser(description='')
    parser.add_argument("--release", "-r", dest="release", required=True,
                        help="Ex: rawhide or f33")
    parser.add_argument("--task-id", "-t", dest="task_ids", action="append", default=[],
                        type=int, help="TaskID of rpms to be installed on qcow2")
    parser.add_argument("--additional-task-id", dest="additional_task_ids", action="append",
                        type=int, default=[], help="extra taskIDs will be avaiblabe as repo, but won't be installed by --install-rpms")
    parser.add_argument("--artifacts", "-a", dest="artifacts", default="./",
                        help="Path where logs, qcow2 and other files will be stored")
    parser.add_argument("--install-rpms", dest="install_rpms", action="store_true")
    parser.add_argument("--no-sys-update", dest="sys_update", action="store_false")
    parser.add_argument("--verbose", "-v", dest="verbose", action="store_true")
    args = parser.parse_args()

    args.release = args.release.lower()

    if not os.path.isdir(args.artifacts):
        os.makedirs(args.artifacts)

    this.artifacts = os.path.abspath(args.artifacts)
    this.result_file = "{}/virt-customize.json".format(this.artifacts)
    this.output_log = "{}/virt-customize.log".format(this.artifacts)

    configure_logging(verbose=args.verbose, output_file=this.output_log)

    task_repos = []
    mykoji = Koji()
    repo_task_ids = args.task_ids + args.additional_task_ids
    if repo_task_ids:
        for task_id in repo_task_ids:
            base_path = "{}/task_repos".format(this.artifacts)
            task_repo = "{}/{}".format(base_path, task_id)
            mykoji.download_task(task_id, task_repo)
            create_repo(task_repo)
            task_repos.append(task_repo)

    image = download_qcow2(args.release)
    if not image:
        raise Exception("Couldn't download qcow2 for {}".format(args.release))

    if not verify_qcow2(image):
        raise Exception("{} is corrupted".format(image))

    qcow2 = Qcow2()
    if not qcow2.prepare_qcow2(image, args.release, task_repos, args.task_ids, args.install_rpms, args.sys_update):
        raise Exception("Couldn't prepare qcow2 image")

    image_path = image
    this.result = {"status": 0, "image": image_path, "log": this.output_log}
    with open(this.result_file, "w") as _file:
        json.dump(this.result, _file, indent=4, sort_keys=True, separators=(',', ': '))


if __name__ == "__main__":
    try:
        main()
    except Exception as exception:
        this.logger.debug(traceback.format_exc())
        this.logger.error(str(exception))
        this.result = {"status": 1, "image": None, "error_reason": str(exception), "log": this.output_log}
        with open(this.result_file, "w") as _file:
            json.dump(this.result, _file, indent=4, sort_keys=True, separators=(',', ': '))
        sys.exit(1)
    sys.exit(0)
