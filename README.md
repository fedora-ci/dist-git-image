# dist-git container image

This repository contains the scripts needed to build a container image for Fedora CI dist-git pipeline.

## Scripts

### virt-customize.py

This script prepares a qcow2 image to be used by the pipeline

* Download the base Fedora Qcow2 image for the release
* Download the rpms from specified Koji task ids
* Copy those rpms and enable them as repositories in the qcow2
* Install the rpms in the qcow2 (optional)
* Update the system (optional)

