import boto3
import json
import logging
import os

from botocore.exceptions import ClientError

import stepfn_strings as strs
import util

logger = logging.getLogger()
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('botocore').setLevel(logging.WARNING)

# this adds vendored directory to the Python import path
here = os.path.dirname(os.path.realpath(__file__))
mocks_dir = os.path.join(here, 'resources', 'mock_data')

ec2 = boto3.client('ec2')
iam = boto3.client('iam')


def get_instance_profile_arn(instance_profile):
    '''
    Returns the ARN of instance_profile
    '''
    if instance_profile[:12] == 'arn:aws:iam:':
        return instance_profile
    logger.info('Fetching arn for instance-profile {}'.format(instance_profile))
    return iam.get_instance_profile(InstanceProfileName=instance_profile)['InstanceProfile']['Arn']


def security_group_id(secgrp):
    '''
    Returns the security group id of secgrp
    Raises an exception if multiple group ids are detected

    Used for EC2-Classic or Default-VPC Launch Configs
    '''
    if secgrp[:3] == 'sg-':
        return secgrp
    resp = ec2.describe_security_groups(GroupNames=[secgrp])
    if len(resp['SecurityGroups']) > 1:
        raise Exception('More than one security group detected for {}'.format(secgrp))
    return resp['SecurityGroups'][0]['GroupId']


def gen_launch_specification(launch_config, avail_zone, subnet_id):
    '''
    Uses an autoscaling launch configuration to generate an EC2 launch specification
    Returns a dict
    '''
    logger.debug('Converting asg launch config to ec2 launch spec')
    # logger.debug('Launch Config: {}'.format(json.dumps(launch_config, indent=2, default=util.json_dumps_converter)))
    spot_launch_specification = {
        'Placement': {
            'AvailabilityZone': avail_zone,
            'Tenancy': launch_config.get('PlacementTenancy', 'default')
        }
    }
    if subnet_id:
        spot_launch_specification['SubnetId'] = subnet_id
    # common keys
    for k in ['AssociatePublicIpAddress', 'BlockDeviceMappings', 'EbsOptimized', 'ImageId',
              'InstanceType', 'KernelId', 'KeyName', 'RamdiskId', 'UserData']:
        if launch_config.get(k):
            spot_launch_specification[k] = launch_config[k]
    # some translation needed ...
    if launch_config.get('IamInstanceProfile'):
        spot_launch_specification['IamInstanceProfile'] = {
            'Arn': get_instance_profile_arn(launch_config['IamInstanceProfile'])
        }
    if launch_config.get('SecurityGroups'):
        spot_launch_specification['SecurityGroupIds'] = [security_group_id(x) for x in launch_config['SecurityGroups']]
    if launch_config.get('InstanceMonitoring'):
        spot_launch_specification['Monitoring'] = {
            'Enabled': launch_config['InstanceMonitoring'].get('Enabled', False)
        }
    # logger.debug('Launch Specification: {}'.format(json.dumps(spot_launch_specification, indent=2, default=util.json_dumps_converter)))
    return spot_launch_specification


def request_spot_instance(launch_config, avail_zone, subnet_id, client_token):
    '''
    Requests a spot instance
    Returns a dict containing the spot instance request response
    '''
    logger.info('Requesting spot instance in {0}/{1}'.format(avail_zone, subnet_id))
    launch_spec = gen_launch_specification(launch_config, avail_zone, subnet_id)
    try:
        resp = ec2.request_spot_instances(InstanceCount=1, LaunchSpecification=launch_spec,
                                          Type='one-time', ClientToken=client_token)
    except ClientError as c:
        if c.response['Error']['Code'] == 'MaxSpotInstanceCountExceeded':
            logger.warning(c.response['Error']['Message'])
            return {'SpoptimizeError': 'MaxSpotInstanceCountExceeded'}
        raise
    logger.debug('Spot request response: {}'.format(json.dumps(resp, indent=2, default=util.json_dumps_converter)))
    return {'SpotInstanceRequestId': resp['SpotInstanceRequests'][0]['SpotInstanceRequestId']}


def get_spot_request_status(spot_request_id):
    '''
    Fetches the spot instance request status of spot_request_id
    Returns instance-id of spot instance if running; 'Pending' or 'Failure' otherwise
    '''
    logger.debug('Checking status of spot request {}'.format(spot_request_id))
    try:
        resp = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
    except ClientError as c:
        if c.response['Error']['Code'] == 'InvalidSpotInstanceRequestID.NotFound':
            logger.info('Spot instance request {} does not exist'.format(spot_request_id))
            return strs.spot_request_failure
        raise
    # logger.debug('Spot request status response: {}'.format(resp))
    spot_request = resp['SpotInstanceRequests'][0]
    if spot_request.get('State', '') == 'active' and spot_request.get('InstanceId'):
        logger.info('Spot instance request {0} is active: {1}'.format(spot_request_id, spot_request['InstanceId']))
        return spot_request['InstanceId']
    if spot_request.get('State', 'unknown') in ['closed', 'cancelled', 'failed']:
        logger.info('Spot instance request {0} is {1}'.format(spot_request_id, spot_request['State']))
        return strs.spot_request_failure
    logger.info('Spot instance request {0} is pending with state {1}'.format(spot_request_id, spot_request['State']))
    return strs.spot_request_pending
