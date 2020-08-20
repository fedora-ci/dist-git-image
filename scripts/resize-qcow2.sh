#!/bin/bash

# using pyhton for resize doesn't seem to be recommended
# https://www.redhat.com/archives/libguestfs/2011-October/msg00092.html

msg_usage() {
    cat << EOF
Increase size of qcow2 image.

Usage:
$PROG <options>

Options:
-i, --image             path to qcow2 image
-s, --size              new size of the image
-p, --partition         resize especific partition (optional)
-h, --help              display this help and exit
EOF
}

# http://wiki.bash-hackers.org/howto/getopts_tutorial
opt_str="$@"
opt=$(getopt -n "$0" --options "hisp" --longoptions "help,image,size,partition:")
while [[ $# -gt 0 ]]; do
    case "$1" in
        -i|--image)
            imageName="$2"
            shift 2
            ;;
        -s|--size)
            increase="$2"
            shift 2
            ;;
        -p|--partition)
            partition="$2"
            shift 2
            ;;
        -h|--help)
            msg_usage
            exit 0
            ;;
        *)
            echo "Invalid option $1"
            msg_usage
            exit 1
    esac
done

if [ -z "${imageName}" ]; then
    echo "imageName parameter not provided. Exiting..."
    exit 1
fi

if [ -z "${increase}" ]; then
    echo "size parameter not provided. Exiting..."
    exit 1
fi

if [ -z "${partition}" ]; then
    # Resize the partition with biggest filesystem
    partition=$(LIBGUESTFS_BACKEND=direct virt-filesystems --partitions --long -a ${imageName} | grep partition | sort -nk4 | tail -n 1 | awk '{print$1}')
fi

set -ex

baseImageName=$(basename ${imageName})
dirImage=$(dirname ${imageName})

qemu-img resize ${imageName} +${increase}

cp ${dirImage}/${baseImageName} ${dirImage}/orig_${baseImageName}

LIBGUESTFS_BACKEND=direct virt-resize --expand ${partition} ${dirImage}/orig_${baseImageName} ${imageName}

qemu-img check ${imageName}

rm -f ${dirImage}/orig_${baseImageName}

# Compresss qcow2
qemu-img convert -c -O qcow2 ${imageName} ${dirImage}/compressed_${baseImageName}

qemu-img check ${dirImage}/compressed_${baseImageName}

mv -f ${dirImage}/compressed_${baseImageName} ${imageName}

qemu-img info ${imageName}
