#!/usr/bin/env python3
"""
Measure the boot speed of cloud instances.

Copyright 2019 Canonical Ltd.
Paride Legovini <paride.legovini@canonical.com>
"""

import argparse
import datetime as dt
import glob
import json
import logging
import platform
import tarfile
import tempfile
import time
import os
import pycloudlib
import shutil
import sys

from pathlib import Path


known_clouds = ['kvm', 'lxd', 'ec2', 'gce']
job_timestamp = dt.datetime.utcnow()


class EC2Instspec:
    cloud = 'ec2'

    def __init__(
            self, *, release, inst_type, region, ec2_subnetid, ec2_sgid,
            ec2_availability_zone, ssh_pubkey_path, ssh_privkey_path,
            ssh_keypair_name):
        # Defaults. They can't be set as keyword argument defaults because
        # we're always passing all the arguments to __init__, even if they
        # are None. And they can't be set as the argparse default values,
        # as different coulds need different defaults.
        self.region = "us-east-1"
        self.inst_type = "t2.micro"
        self.subnetid = ec2_subnetid
        self.sgid = ec2_sgid
        self.availability_zone = ec2_availability_zone
        self.ssh_pubkey_path = ssh_pubkey_path
        self.ssh_privkey_path = ssh_privkey_path
        self.ssh_keypair_name = ssh_keypair_name

        # User-specified settings
        self.release = release

        if inst_type:
            self.inst_type = inst_type
        if region:
            self.region = region

    def measure(self, datadir, instances=1, reboots=1):
        """
        Measure Amazon AWS EC2.
        Returns the measurement metadata as a dictionary
        """
        print('Perforforming measurement on Amazon EC2')

        ec2 = pycloudlib.EC2(tag='bootspeed', region=self.region)

        if not self.ssh_pubkey_path:
            self.ssh_pubkey_path = ec2.key_pair.public_key_path
        if not self.ssh_privkey_path:
            self.ssh_privkey_path = ec2.key_pair.private_key_path
        if not self.ssh_keypair_name:
            self.ssh_keypair_name = ec2.key_pair.name
        ec2.use_key(self.ssh_pubkey_path, self.ssh_privkey_path,
                    self.ssh_keypair_name)

        if self.inst_type.split('.')[0] == 'a1':
            if self.release == 'xenial' or self.release == 'bionic':
                # Workaround for LP: #1832386
                daily = ec2.released_image(release=self.release, arch='arm64')
            else:
                daily = ec2.daily_image(release=self.release, arch='arm64')
        else:
            daily = ec2.daily_image(release=self.release)

        serial = ec2.image_serial(daily)

        print("Daily image for", self.release, "is", daily)
        print("Image serial:", serial)

        for ninstance in range(instances):
            instance_data = Path(datadir, "instance_" + str(ninstance))
            instance_data.mkdir()

            # This tag name will be inherited by the launched instance.
            # We want it to be unique and to contain an easily parsable
            # timestamp (UTC seconds since epoch), which we will use to
            # detemine if an instance is stale and should be terminated.
            tag = "bootspeed-" + str(int(dt.datetime.utcnow().timestamp()))
            ec2.tag = tag

            print("Launching instance", ninstance+1,
                  "of", instances, "tag:", ec2.tag)
            instance = ec2.launch(
                daily,
                instance_type=self.inst_type,
                SubnetId=self.subnetid,
                SecurityGroupIds=self.sgid,
                Placement={
                    'AvailabilityZone': self.availability_zone
                }
            )
            print("Instance launched.")

            # If the availability zone is not specified a random one is
            # assigned. We want to make sure the next instances (if any) will
            # use the same zone, so we save it.
            if not self.availability_zone:
                self.availability_zone = instance.availability_zone

            try:
                measure_instance(instance, instance_data, reboots)
            finally:
                print("Deleting the instance.")
                instance.delete(wait=False)

        metadata = gen_metadata(
            cloud=self.cloud, region=self.region,
            availability_zone=self.availability_zone, inst_type=self.inst_type,
            release=self.release, cloudid=daily, serial=serial)

        return metadata


class LXDInstspec:
    cloud = 'lxd'

    def __init__(self, *, release, inst_type):
        self.inst_type = ""
        self.release = release

        if inst_type:
            self.inst_type = inst_type

    def measure(self, datadir, instances=1, reboots=1):
        """
        Measure LXD containers.
        Returns the measurement metadata as a dictionary
        """
        print('Perforforming measurement on LXD')

        lxd = pycloudlib.LXD(tag='bootspeed')
        image = lxd.daily_image(release=self.release)
        serial = lxd.image_serial(image)

        for ninstance in range(instances):
            instance_data = Path(datadir, "instance_" + str(ninstance))
            instance_data.mkdir()

            name = "bootspeed-" + str(int(dt.datetime.utcnow().timestamp()))

            print("Launching instance", ninstance+1, "of", instances)
            instance = lxd.launch(name, image, inst_type=self.inst_type)
            print("Instance launched.")

            try:
                measure_instance(instance, instance_data, reboots)
            finally:
                print("Deleting the instance.")
                instance.delete()

        # On LXD we can consider the machine the measurement is run on as the
        # 'region'; platform.node() returns its hostname.
        region = platform.node()
        metadata = gen_metadata(
            cloud=self.cloud, region=region, inst_type=self.inst_type,
            release=self.release, cloudid=image, serial=serial)

        return metadata


class KVMInstspec:
    cloud = 'kvm'

    def __init__(self, *, release, inst_type):
        self.inst_type = ""
        self.release = release

        if inst_type:
            self.inst_type = inst_type

    def measure(self, datadir, instances=1, reboots=1):
        """
        Measure KVM instances.
        Returns the measurement metadata as a dictionary
        """
        print('Perforforming measurement on KVM')

        kvm = pycloudlib.KVM(tag='bootspeed')
        image = kvm.daily_image(release=self.release)
        serial = kvm.image_serial(image)

        for ninstance in range(instances):
            instance_data = Path(datadir, "instance_" + str(ninstance))
            instance_data.mkdir()

            name = "bootspeed-" + str(int(dt.datetime.utcnow().timestamp()))

            print("Launching instance", ninstance+1, "of", instances)
            instance = kvm.launch(name, image, inst_type=self.inst_type)
            print("Instance launched.")

            try:
                measure_instance(instance, instance_data, reboots)
            finally:
                print("Deleting the instance.")
                instance.delete()

        # On KVM we can consider the machine the measurement is run on as the
        # 'region'; platform.node() returns its hostname.
        region = platform.node()
        metadata = gen_metadata(
            cloud=self.cloud, region=region, inst_type=self.inst_type,
            release=self.release, cloudid=image, serial=serial)

        return metadata


def measure_instance(instance, datadir, reboots=1):
    print("*** Measuring instance ***")

    # Use the same command (and hence format) used when measuring devices
    os.system("date --utc --rfc-3339=ns > " +
              str(Path(datadir, "job-start-timestamp")))

    # Do not refresh the snaps for the moment.
    # Regular Ubuntu Server images do not auto-reboot on snap refreshes as Core
    # does, but we want to keep the measurement scripts as similar as possible.

    instance.execute(
        'sudo snap set system refresh.hold='
        '"$(date --date=tomorrow +%Y-%m-%dT%H:%M:%S%:z)"')
    instance.execute(
        "wget https://raw.githubusercontent.com/CanonicalLtd/"
        "server-test-scripts/master/boot-speed/bootspeed.sh </dev/null")
    instance.execute("chmod +x bootspeed.sh")
    instance.execute("rm -rf artifacts")
    outstr = instance.execute("./bootspeed.sh 2>&1")
    print(outstr)
    outstr = instance.execute("find artifacts")
    print(outstr)

    # Test for the existence of the file bootspeed.sh creates if it reached to
    # the end of the measurement with no errors.
    outstr = instance.execute("test -f artifacts/measurement-successful"
                              " && echo ok")
    if outstr != "ok":
        print("Measurement failed (missing measurement-successful)!")
        sys.exit(1)

    instance.execute("mv artifacts boot_0")
    instance.execute("tar czf boot_0.tar.gz boot_0")
    instance.pull_file("boot_0.tar.gz", "boot_0.tar.gz")
    instance.execute("sudo snap refresh")

    for nboot in range(1, reboots+1):
        bootdir = "boot_" + str(nboot)

        if instance._type == 'kvm':
            # Ugly workaround for:
            # https://github.com/CanonicalLtd/multipass/issues/903
            try:
                instance.restart()
            except RuntimeError:
                pass
            time.sleep(120)
        else:
            instance.restart()

        instance.execute("rm -rf artifacts")
        outstr = instance.execute("./bootspeed.sh 2>&1")
        print(outstr)
        instance.execute("mv artifacts " + bootdir)
        instance.execute("tar czf " + bootdir + ".tar.gz " + bootdir)
        instance.pull_file(bootdir + ".tar.gz", bootdir + ".tar.gz")

    for tarball in glob.glob('boot_*.tar.gz'):
        with tarfile.open(tarball, "r:gz") as tar:
            tar.extractall(path=datadir)
        os.unlink(tarball)


def gen_metadata(
        *, cloud, region, availability_zone='', inst_type, release, cloudid,
        serial):
    """ Returns the instance metadata as a dictionary """
    date = job_timestamp.strftime('%Y%m%d%H%M%S')
    isodate = job_timestamp.isoformat()

    metadata = {}
    metadata['date'] = date
    metadata['date-rfc3339'] = isodate
    metadata['type'] = "cloud"
    metadata['instance'] = {
        'cloud': cloud,
        'region': region,
        'availability_zone': availability_zone,
        'instance_type': inst_type,
        'release': release,
        'cloudimage_id': cloudid,
        'image_serial': serial,
    }

    return metadata


def gen_archivename(metadata):
    """ Generate a standardized measurement directory (and tarball) name """
    date = metadata['date']
    cloud = metadata['instance']['cloud']
    inst_type = metadata['instance']['instance_type']
    release = metadata['instance']['release']

    arcname = cloud + "-" + inst_type + "-" + release + "_" + date
    return arcname


def main():
    args = parse_args()

    if args.cloud not in known_clouds:
        print('Unknown cloud provider:', args.cloud)
        sys.exit(1)

    if args.cloud == 'ec2':
        instspec = EC2Instspec(
            release=args.release, inst_type=args.inst_type, region=args.region,
            ec2_subnetid=args.ec2_subnetid, ec2_sgid=args.ec2_sgid,
            ec2_availability_zone=args.ec2_availability_zone,
            ssh_pubkey_path=args.ssh_pubkey_path,
            ssh_privkey_path=args.ssh_privkey_path,
            ssh_keypair_name=args.ssh_keypair_name)
    elif args.cloud == 'lxd':
        instspec = LXDInstspec(release=args.release, inst_type=args.inst_type)
    elif args.cloud == 'kvm':
        instspec = KVMInstspec(release=args.release, inst_type=args.inst_type)
    else:
        raise NotImplementedError

    tmp_datadir = tempfile.mkdtemp(prefix='bootspeed-', dir=os.getcwd())

    logging.basicConfig(level=logging.INFO)
    metadata = instspec.measure(tmp_datadir, args.instances, args.reboots)

    with open(Path(tmp_datadir, "metadata.json"), 'w') as mdfile:
        json.dump(metadata, mdfile)

    archivename = gen_archivename(metadata)
    with tarfile.open((archivename + ".tar.gz"), "w:gz") as tar:
        tar.add(tmp_datadir, arcname=archivename)

    shutil.rmtree(tmp_datadir)


def parse_args():
    PARSER = argparse.ArgumentParser()
    PARSER.add_argument('-c', '--cloud', help='Cloud to measure',
                        choices=known_clouds, required=True)
    PARSER.add_argument('-t', '--inst-type', help='Instance type')
    PARSER.add_argument('-r', '--release',
                        help='Ubuntu release to measure', required=True)
    PARSER.add_argument('--reboots', help='Number of reboots',
                        default=1, type=int)
    PARSER.add_argument('--instances', help='Number of instances',
                        default=1, type=int)
    PARSER.add_argument('--ssh-pubkey-path', help="Override pycloudlib's "
                        "default for the SSH public key to use", default=None)
    PARSER.add_argument('--ssh-privkey-path', help="Override pycloudlib's "
                        "default for the SSH private key to sue", default=None)
    PARSER.add_argument('--ssh-keypair-name', help="Override pycloudlib's "
                        " default for the SSH keypair name", default=None)
    PARSER.add_argument('--ec2-subnetid', help='AWS EC2 SubnetId', default='')
    PARSER.add_argument('--ec2-availability-zone',
                        help='AWS EC2 Availability Zone', default='')
    PARSER.add_argument(
        '--ec2-sgid', help='AWS EC2 SecurityGroupId', action='append',
        default=[])
    PARSER.add_argument('--region', help='Cloud region')
    ARGS = PARSER.parse_args()
    return ARGS


if __name__ == '__main__':
    main()
