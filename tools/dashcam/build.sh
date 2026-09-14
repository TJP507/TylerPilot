#!/bin/sh
# Build the Qualcomm msm_vidc HEVC decoder helper for comma 3X (aarch64).
#
# The compiled binary is checked in at tools/dashcam/hwdec so devices can use it
# without a build toolchain. Rebuild with this script on a device (or an aarch64
# host with kernel headers) and commit the result.
set -e
cd "$(dirname "$0")"
g++ -O2 -s -Wall -o hwdec hwdec.cc -lpthread
echo "built $(pwd)/hwdec"
