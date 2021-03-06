#!/usr/bin/env python3

import collections
import itertools
import tempfile
import requests
import netaddr
import random
import string
import base64
import time
import sys
import re
import os
from subprocess import check_call, call

import boto3
import click
from botocore.exceptions import ClientError
from clickclick import Action, info

from .common import override_ephemeral_block_devices, \
    dump_user_data_for_taupage, setup_sns_topics_for_alarm, \
    create_auto_recovery_alarm, ensure_instance_profile


def setup_security_groups(use_dmz: bool, cluster_name: str, node_ips: dict,
                          result: dict) -> dict:
    '''
    Allow traffic between regions (or within a VPC, if `use_dmz' is False)
    '''
    description = 'Allow Cassandra nodes to talk to each other on port 7001'
    for region, ips in node_ips.items():
        with Action('Configuring Security Group in {}..'.format(region)):
            ec2 = boto3.client('ec2', region)
            resp = ec2.describe_vpcs()
            # TODO: support more than one VPC..
            vpc = resp['Vpcs'][0]
            sg_name = cluster_name
            sg = ec2.create_security_group(
                GroupName=sg_name,
                VpcId=vpc['VpcId'],
                Description=description
            )
            result[region] = sg

            ec2.create_tags(
                Resources=[sg['GroupId']],
                Tags=[{'Key': 'Name', 'Value': sg_name}]
            )
            ip_permissions = []
            if use_dmz:
                # NOTE: we need to allow ALL public IPs (from all regions)
                for ip in itertools.chain(*node_ips.values()):
                    ingress_rule = {
                        'IpProtocol': 'tcp',
                        'FromPort': 7001,  # port range: From-To
                        'ToPort':   7001,
                        'IpRanges': [{
                            'CidrIp': '{}/32'.format(ip['PublicIp'])
                        }]
                    }
                    ip_permissions.append(ingress_rule)
            # if internal subnets are used we just allow access from
            # within the SG, which we also need in multi-region setup
            # (for the nodetool?)
            self_ingress_rule = {
                'IpProtocol': '-1',
                'UserIdGroupPairs': [{'GroupId': sg['GroupId']}]
            }
            ip_permissions.append(self_ingress_rule)

            # if we can find the Odd security group, authorize SSH access from it
            try:
                resp = ec2.describe_security_groups(
                    GroupNames=['Odd (SSH Bastion Host)']
                )
                odd_sg = resp['SecurityGroups'][0]
                odd_ingress_rule = {
                    'IpProtocol': 'tcp',
                    'FromPort': 22,  # port range: From-To
                    'ToPort': 22,
                    'UserIdGroupPairs': [{
                        'GroupId': odd_sg['GroupId']
                    }]
                }
                ip_permissions.append(odd_ingress_rule)
            except ClientError:
                msg = "No Odd host in region {}, skipping Security Group rule."
                info(msg.format(region))
                pass

            ec2.authorize_security_group_ingress(
                GroupId=sg['GroupId'],
                IpPermissions=ip_permissions
            )


def find_taupage_amis(regions: list) -> dict:
    '''
    Find latest Taupage AMI for each region
    '''
    result = {}
    for region in regions:
        with Action('Finding latest Taupage AMI in {}..'.format(region)):
            ec2 = boto3.resource('ec2', region)
            filters = [
                {'Name': 'name', 'Values': ['*Taupage-AMI-*']},
                {'Name': 'is-public', 'Values': ['false']},
                {'Name': 'state', 'Values': ['available']},
                {'Name': 'root-device-type', 'Values': ['ebs']}
            ]
            images = list(ec2.images.filter(Filters=filters))
            if not images:
                raise Exception('No Taupage AMI found')
            most_recent_image = sorted(images, key=lambda i: i.name)[-1]
            result[region] = most_recent_image
        info(most_recent_image.name)
    return result


def get_latest_docker_image_version(artifact_name):
    url = 'https://registry.opensource.zalan.do/teams/stups/artifacts/{}/tags' \
          .format(artifact_name)
    return requests.get(url).json()[-1]['name']


password_chars = "{}{}{}".format(
    string.ascii_letters, string.digits,
    re.sub("[\\\\'\"]", "", string.punctuation)
)


def generate_password(length: int = 32) -> str:
    return "".join(random.choice(password_chars) for x in range(length))


def generate_certificate(cluster_name: str):
    check = call(["which", "keytool"])
    if check:
        print("Keytool is not in searchpath")
        return

    d = tempfile.mkdtemp()
    try:
        keystore = os.path.join(d, 'keystore')
        cmd = [
            "keytool", "-genkeypair",
            "-alias", "planb",
            "-keyalg", "RSA",
            "-validity", "36000",
            "-keystore", keystore,
            "-dname", "c=DE, st=Berlin, l=Berlin, o=Zalando SE, cn=zalando.net",
            "-storepass", cluster_name,
            "-keypass", cluster_name
        ]
        check_call(cmd)
        cert = os.path.join(d, 'cert')
        export = [
            "keytool", "-export",
            "-alias", "planb",
            "-keystore", keystore,
            "-rfc",
            "-file", cert,
            "-storepass", cluster_name
        ]
        check_call(export)
        truststore = os.path.join(d, 'truststore')
        importcmd = [
            "keytool", "-import",
            "-noprompt",
            "-alias", "planb",
            "-file", cert,
            "-keystore", truststore,
            "-storepass", cluster_name
        ]
        check_call(importcmd)

        with open(keystore, 'rb') as fd:
            keystore_data = fd.read()
        with open(truststore, 'rb') as fd:
            truststore_data = fd.read()
    finally:
        pass
    return keystore_data, truststore_data


class IpAddressPoolDepletedException(Exception):

    def __init__(self, cidr_block: str):
        msg = "Pool of unused IP addresses depleted in subnet: {}".format(cidr_block)
        super(IpAddressPoolDepletedException, self).__init__(msg)


def generate_private_ip_addresses(ec2: object, subnets: list, cluster_size: int):

    def try_next_address(ips, subnet):
        try:
            return str(next(ips))
        except StopIteration:
            raise IpAddressPoolDepletedException(subnet['CidrBlock'])

    #
    # Here we have to account for the behavior of launch_*_nodes
    # which iterate through subnets to put the instances into
    # different Availability Zones.
    #
    network_ips = [
        netaddr.IPNetwork(s['CidrBlock']).iter_hosts()
        for s in subnets
    ]
    for idx, ips in enumerate(network_ips):
        #
        # Some of the first addresses in each subnet are
        # taken by AWS system instances that we can't see,
        # so we try to skip them.
        #
        for _ in range(10):
            try_next_address(ips, subnets[idx])

    i = 0
    while i < cluster_size:
        idx = i % len(subnets)

        ip = try_next_address(network_ips[idx], subnets[idx])

        resp = ec2.describe_instances(
            Filters=[{
                'Name': 'private-ip-address',
                'Values': [ip]
            }]
        )
        if not resp['Reservations']:
            i += 1
            yield ip


def allocate_ip_addresses(
        region_subnets: dict, cluster_size: int,
        node_ips: dict, take_elastic_ips: bool):
    '''
    Allocate unused private IP addresses by checking the current
    reservations, and optionally allocate Elastic IPs.
    '''
    for region, subnets in region_subnets.items():
        with Action('Allocating IP addresses in {}..'.format(region)) as act:
            ec2 = boto3.client('ec2', region_name=region)

            for ip in generate_private_ip_addresses(ec2, subnets, cluster_size):
                address = {'PrivateIp': ip}

                if take_elastic_ips:
                    resp = ec2.allocate_address(Domain='vpc')
                    address['_defaultIp'] = resp['PublicIp']
                    address['PublicIp'] = resp['PublicIp']
                    address['AllocationId'] = resp['AllocationId']
                else:
                    address['_defaultIp'] = ip

                node_ips[region].append(address)
                act.progress()


def pick_seed_node_ips(node_ips: dict, seed_count: int) -> dict:
    '''
    Take first {seed_count} IPs in every region for the seed nodes.
    '''
    seed_nodes = {}
    for region, ips in node_ips.items():
        seed_nodes[region] = ips[0:seed_count]

        list_ips = ', '.join([ip['_defaultIp'] for ip in seed_nodes[region]])
        info('Our seed nodes in {} will be: {}'.format(region, list_ips))
    return seed_nodes


def get_subnets(prefix_filter: str, regions: list) -> dict:
    '''
    Returns a dict of per-region lists of subnets, which names start
    with the specified prefix (it should be either 'dmz-' or
    'internal-'), sorted by the Availability Zone.
    '''
    subnets = collections.defaultdict(list)
    for region in regions:
        ec2 = boto3.client('ec2', region)
        resp = ec2.describe_subnets()
        sorted_subnets = sorted(
            resp['Subnets'],
            key=lambda subnet: subnet['AvailabilityZone']
        )
        for subnet in sorted_subnets:
            for tag in subnet['Tags']:
                if tag['Key'] == 'Name':
                    if tag['Value'].startswith(prefix_filter):
                        subnets[region].append(subnet)
    return subnets


def hostname_from_private_ip(region: str, ip: str) -> str:
    return 'ip-{}.{}.compute.internal.'.format('-'.join(ip.split('.')), region)


def make_dns_records(region: str, ips: list) -> list:
    hosts = [hostname_from_private_ip(region, ip['PrivateIp']) for ip in ips]
    return [{'Value': '1 1 9042 {}'.format(host)} for host in hosts]


def setup_dns_records(cluster_name: str, hosted_zone: str, node_ips: dict):
    r53 = boto3.client('route53')

    zone = None
    zones = r53.list_hosted_zones_by_name(DNSName=hosted_zone)
    for z in zones['HostedZones']:
        if z['Name'] == hosted_zone:
            zone = z
    if not zone:
        raise Exception('Failed to find Hosted Zone {}'.format(hosted_zone))

    for region, ips in node_ips.items():
        with Action('Setting up Route53 SRV records in {}..'.format(region)):
            name = '_{}-{}._tcp.{}'.format(cluster_name, region, hosted_zone)
            #
            # NB: We always want the clients to connect using private
            # IP addresses.
            #
            # But we must record the host names, otherwise the client
            # will get the addresses ending with the dot from the DSN
            # lookup and won't recognize them as such.
            #
            records = make_dns_records(region, ips)

            r53.change_resource_record_sets(
                HostedZoneId=zone['Id'],
                ChangeBatch={
                    'Changes': [{
                        'Action': 'UPSERT',
                        'ResourceRecordSet': {
                            'Name': name,
                            'Type': 'SRV',
                            'TTL': 60,
                            'ResourceRecords': records
                        }
                    }]
                }
            )


def generate_taupage_user_data(options: dict) -> str:
    '''
    Generate Taupage user data to start a Cassandra node
    http://docs.stups.io/en/latest/components/taupage.html
    '''
    keystore_base64 = base64.b64encode(options['keystore'])
    truststore_base64 = base64.b64encode(options['truststore'])

    # seed nodes across all regions
    all_seeds = [
        ip['_defaultIp']
        for region, ips in options['seed_nodes'].items()
        for ip in ips
    ]
    data = {
        'runtime': 'Docker',
        'source': options['docker_image'],
        'application_id': options['cluster_name'],
        'application_version': options['image_version'],
        'networking': 'host',
        'ports': {
            '7001': '7001',
            '9042': '9042'
        },
        'environment': {
            'CLUSTER_NAME': options['cluster_name'],
            'CLUSTER_SIZE': options['cluster_size'],
            'NUM_TOKENS': options['num_tokens'],
            'REGIONS': ' '.join(options['regions']),
            'SUBNET_TYPE': 'dmz' if options['use_dmz'] else 'internal',
            'SEEDS': ','.join(all_seeds),
            'KEYSTORE': str(keystore_base64, 'UTF-8'),
            'TRUSTSTORE': str(truststore_base64, 'UTF-8'),
            'ADMIN_PASSWORD': generate_password()
        },
        'volumes': {
            'ebs': {
                '/dev/xvdf': None
            }
        },
        'mounts': {
            '/var/lib/cassandra': {
                'partition': '/dev/xvdf',
                'options': 'noatime,nodiratime'
            }
        },
        'scalyr_account_key': options['scalyr_key']
    }

    if options['environment']:
        data['environment'].update(options['environment'])

    return data


def create_tagged_volume(ec2: object, options: dict, zone: str, name: str):
    ebs_data = {
        "AvailabilityZone": zone,
        "VolumeType": options['volume_type'],
        "Size": options['volume_size'],
        "Encrypted": False,
    }
    if options['volume_type'] == 'io1':
        ebs_data['Iops'] = options['volume_iops']
    vol = ec2.create_volume(**ebs_data)

    tags = [
        {'Key': 'Name', 'Value': name},
        {'Key': 'Taupage:erase-on-boot', 'Value': 'True'}
    ]
    ec2.create_tags(Resources=[vol['VolumeId']], Tags=tags)


def launch_instance(region: str, ip: dict, ami: object, subnet: dict,
                    security_group_id: str, is_seed: bool, options: dict):

    node_type = 'SEED' if is_seed else 'NORMAL'
    msg = 'Launching {} node {} in {}..'.format(
        node_type,
        ip['_defaultIp'],
        region
    )
    with Action(msg) as act:
        ec2 = boto3.client('ec2', region_name=region)

        mappings = ami.block_device_mappings
        block_devices = override_ephemeral_block_devices(mappings)

        volume_name = '{}-{}'.format(options['cluster_name'], ip['PrivateIp'])
        create_tagged_volume(
            ec2,
            options,
            subnet['AvailabilityZone'],
            volume_name
        )

        user_data = options['user_data']
        user_data['volumes']['ebs']['/dev/xvdf'] = volume_name
        taupage_user_data = dump_user_data_for_taupage(user_data)

        resp = ec2.run_instances(
            ImageId=ami.id,
            MinCount=1,
            MaxCount=1,
            SecurityGroupIds=[security_group_id],
            UserData=taupage_user_data,
            InstanceType=options['instance_type'],
            SubnetId=subnet['SubnetId'],
            PrivateIpAddress=ip['PrivateIp'],
            BlockDeviceMappings=block_devices,
            IamInstanceProfile={'Arn': options['instance_profile']['Arn']},
            DisableApiTermination=not(options['no_termination_protection'])
        )
        instance = resp['Instances'][0]
        instance_id = instance['InstanceId']

        ec2.create_tags(
            Resources=[instance_id],
            Tags=[{'Key': 'Name', 'Value': options['cluster_name']}]
        )
        # wait for instance to initialize before we can assign a
        # public IP address to it or tag the attached volume
        while True:
            resp = ec2.describe_instances(InstanceIds=[instance_id])
            instance = resp['Reservations'][0]['Instances'][0]
            if instance['State']['Name'] != 'pending':
                break
            time.sleep(5)
            act.progress()

        if options['use_dmz']:
            ec2.associate_address(
                InstanceId=instance_id,
                AllocationId=ip['AllocationId']
            )

        alarm_sns_topic_arn = None
        if options['alarm_topics']:
            alarm_sns_topic_arn = options['alarm_topics'][region]

        create_auto_recovery_alarm(
            region, options['cluster_name'],
            instance_id, alarm_sns_topic_arn
        )


def launch_seed_nodes(options: dict):
    total_seed_count = options['seed_count'] * len(options['regions'])
    seeds_launched = 0
    for region, ips in options['seed_nodes'].items():
        security_group_id = options['security_groups'][region]['GroupId']
        subnets = options['subnets'][region]
        for i, ip in enumerate(ips):
            launch_instance(
                region, ip,
                ami=options['taupage_amis'][region],
                subnet=subnets[i % len(subnets)],
                security_group_id=security_group_id,
                is_seed=True,
                options=options
            )
            seeds_launched += 1
            if seeds_launched < total_seed_count:
                info("Sleeping for a minute before launching next SEED node..")
                time.sleep(60)


def launch_normal_nodes(options: dict):
    # TODO: parallelize by region?
    for region, ips in options['node_ips'].items():
        subnets = options['subnets'][region]
        security_group_id = options['security_groups'][region]['GroupId']
        for i, ip in enumerate(ips):
            if i >= options['seed_count']:
                # avoid stating all nodes at the same time
                info("Sleeping for one minute before launching next node..")
                time.sleep(60)
                launch_instance(
                    region, ip,
                    ami=options['taupage_amis'][region],
                    subnet=subnets[i % len(subnets)],
                    security_group_id=security_group_id,
                    is_seed=False,
                    options=options
                )


def print_success_message(options: dict):
    info('Cluster initialization completed successfully!')
    sys.stdout.write('''
The Cassandra cluster {cluster_name} was created with {cluster_size} nodes
in each of the following AWS regions: {regions_list}

You can now login to any of the cluster nodes with the superuser
account using the following command from inside the docker container:

(docker)$ cqlsh -u admin -p $ADMIN_PASSWORD

From there you can create non-superuser roles and otherwise configure
the cluster.

You might also need to update the Security Groups named {cluster_name}
(in all regions!) to allow access to Cassandra from your application (port 9042)
and optionally to allow access to Jolokia (port 8778) and/or
Prometheus Node Exporter (port 9100) from your monitoring tool.
'''.format(**options, regions_list=' '.join(options['regions'])))


def print_failure_message():
    sys.stderr.write('''
You were trying to deploy Plan B Cassandra, but the process has failed :-(

One of the reasons might be that some of Private IP addresses we were
going to use to launch the EC2 instances were taken by some other
instances in the middle of the process.  If that is the case, simply
retrying the operation might resolve the problem (you still might need
to clean up after this attempt before retrying).

Please review the error message to see if that is the case, then
either correct the error or retry.

''')


def validate_artifact_version(options: dict) -> dict:
    conflict_options_msg = """Conflicting options: --artifact-name and
--docker-image cannot be specified at the same time"""
    if not options['docker_image']:
        if not options['artifact_name']:
            options['artifact_name'] = 'planb-cassandra-3.0'
        image_version = get_latest_docker_image_version(options['artifact_name'])
        docker_image = 'registry.opensource.zalan.do/stups/{}:{}' \
                       .format(options['artifact_name'], image_version)
        info('Using docker image: {}'.format(docker_image))
    else:
        if options['artifact_name']:
            raise click.UsageError(conflict_options_msg)
        image_version = options['docker_image'].split(':')[-1]
        docker_image = options['docker_image']
    return dict(options, docker_image=docker_image, image_version=image_version)


def read_environment(options: dict) -> dict:
    if options['environment']:
        return dict(
            options,
            environment=dict(
                map(lambda x: x.split("=", 1), options['environment'])
            )
        )
    else:
        return options


def create_cluster(options: dict):
    options = validate_artifact_version(options)
    options = read_environment(options)

    keystore, truststore = generate_certificate(options['cluster_name'])

    # List of IP addresses by region
    node_ips = collections.defaultdict(list)

    # Mapping of region name to the Security Group
    security_groups = {}

    try:
        taupage_amis = find_taupage_amis(options['regions'])

        subnets = get_subnets(
            'dmz-' if options['use_dmz'] else 'internal-',
            options['regions']
        )
        allocate_ip_addresses(
            subnets, options['cluster_size'], node_ips,
            take_elastic_ips=options['use_dmz']
        )

        if options['sns_topic'] or options['sns_email']:
            alarm_topics = setup_sns_topics_for_alarm(
                options['regions'],
                options['sns_topic'],
                options['sns_email']
            )
        else:
            alarm_topics = {}

        if options['hosted_zone']:
            setup_dns_records(
                options['cluster_name'],
                options['hosted_zone'],
                node_ips
            )
        setup_security_groups(
            options['use_dmz'],
            options['cluster_name'],
            node_ips,
            security_groups
        )
        # We should have up to 3 seeds nodes per DC
        seed_count = min(options['cluster_size'], 3)
        seed_nodes = pick_seed_node_ips(node_ips, seed_count)

        options = dict(
            options,
            keystore=keystore,
            truststore=truststore,
            seed_count=seed_count,
            seed_nodes=seed_nodes
        )
        user_data = generate_taupage_user_data(options)

        instance_profile = ensure_instance_profile(options['cluster_name'])

        options = dict(
            options,
            node_ips=node_ips,
            security_groups=security_groups,
            taupage_amis=taupage_amis,
            subnets=subnets,
            alarm_topics=alarm_topics,
            user_data=user_data,
            instance_profile=instance_profile
        )
        launch_seed_nodes(options)

        # TODO: make sure all seed nodes are up
        launch_normal_nodes(options)

        print_success_message(options)

    except:
        print_failure_message()

        #
        # TODO: in order to break dependencies, delete entities in the
        # order opposite to the creation.  For that pushing things on
        # Undo stack sounds like a natural choice.
        #
        for region, sg in security_groups.items():
            ec2 = boto3.client('ec2', region)
            info('Cleaning up security group: {}'.format(sg['GroupId']))
            ec2.delete_security_group(GroupId=sg['GroupId'])

        if options['use_dmz']:
            for region, ips in node_ips.items():
                ec2 = boto3.client('ec2', region)
                for ip in ips:
                    info('Releasing IP address: {}'.format(ip['PublicIp']))
                    ec2.release_address(AllocationId=ip['AllocationId'])

        raise
