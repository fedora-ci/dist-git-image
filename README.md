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

### provision.py

* Provision a VM based on qcow2 image using standard-test-roles
* Safe ansible inventory to a file

### virt-customize.py

This script prepares a qcow2 image to be used by the pipeline

* Download the base Fedora Qcow2 image for the release
* Download the rpms from specified Koji task ids
* Copy those rpms and enable them as repositories in the qcow2
* Install the rpms in the qcow2 (optional)
* Update the system (optional)
