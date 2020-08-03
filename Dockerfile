FROM fedora:latest
LABEL maintainer "https://github.com/fedora-ci/dist-git-image.git"
LABEL description="This container is meant to \
contain all script needs to prepare the QCOW2 used as test subject \
to the Fedora-CI pipeline and also run the tests."
USER root

# Install all package requirements
RUN for i in {1..5} ; do dnf -y install \
        ansible \
        createrepo \
        dnf-plugins-core \
        dnf-utils \
        fedpkg \
        git \
        koji \
        krb5-workstation \
        python3-libguestfs \
        python3-libselinux \
        python3-pip \
        # install python3-devel as workaround for https://pagure.io/standard-test-roles/issue/313
        python3-devel \
        python3-dnf \
        qemu-img \
        rpm-build \
        standard-test-roles \
        standard-test-roles-inventory-qemu \
        && dnf clean all \
        && break || sleep 10 ; done

COPY default.xml /etc/libvirt/qemu/networks/
ENV LIBGUESTFS_BACKEND=direct

VOLUME [ "/sys/fs/cgroup" ]

# Copy necessary virt-customize files into container
COPY ["scripts/virt-customize.py", \
# Copy necessary rpmbuild files into the container
#      "scripts/pull_old_task.sh", "scripts/repoquery.sh", "scripts/koji_build_pr.sh", \
# Copy necessary files from package-test to the container
#      "scripts/package-test.sh", "scripts/verify-rpm.sh", "scripts/rpm-verify.yml", \
#      "scripts/resize-qcow2.sh", "scripts/sync-artifacts.yml", \
      "/tmp/"]

# Ansible API changes very often, make sure we run with a version we know it works
RUN pip-3 install ansible==2.8.0

ENV ANSIBLE_INVENTORY=/usr/share/ansible/inventory/standard-inventory-qcow2

ENTRYPOINT ["bash"]
#
# Run the container as follows
# docker run --privileged container_tag
