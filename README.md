# dist-git container image

This repository contains the scripts needed to build a container image for Fedora CI dist-git pipeline.

## Scripts

### checkout-repo.py

This script checkouts specific branch from git repo and check if there is tests/tests*.yml

If PR is specified merge PR patch to branch

* Clone git repo
* Checkout branch
* Fetch PR
* Merge PR

### create-build.py

This script creates an scratch build in koji from a pull request

* Clone git repo
* Checkout branch
* Fetch PR
* Merge PR
* Prepare an src.rpm file
* Submit an scratch build from src.rpm
* Wait scratch build to complete

### merge-results.py

Merge all results.yml from test playbooks to a single file

* Optionally saves the merged result as xunit file

### resize-qcow2.sh

Script used to expand the filesystem on qcow2 image

### run-playbook.py

* Provision a VM based on qcow2 image using standard-test-roles
* Run ansible playbook using VM as inventory

### virt-customize.py

This script prepares a qcow2 image to be used by the pipeline

* Download the base Fedora Qcow2 image for the release
* Download the rpms from specified Koji task ids (optional)
    * Copy those rpms and enable them as repositories in the qcow2
    * Install the rpms in the qcow2 (optional)
* Update the system (optional)

## Example how to run tests locally

1. Start the container

    `podman run -it --rm --privileged quay.io/bgoncalv/fedoraci-dist-git`

2. Clone repository with tests

    `python3 /tmp/checkout-repo.py --repo ksh`

3. Prepare qcow2

    `python3 /tmp/virt-customize.py --release rawhide`

4. Run the tests
    * `cd ksh/tests`
    * `python3 /tmp/run-playbook.py --image /Fedora-Rawhide.qcow2 --playbook tests.yml --artifact ./artifacts`
