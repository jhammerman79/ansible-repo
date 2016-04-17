#!/usr/bin/python
# (c) 2016, Pierre Jodouin <pjodouin@virtualcomputing.solutions>
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import sys
import json
from hashlib import md5

try:
    import boto3
    import boto              # seems to be needed for ansible.module_utils
    from botocore.exceptions import ClientError, ParamValidationError, MissingParametersError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


DOCUMENTATION = '''
---
module: lambda_event
short_description: Creates, updates or deletes AWS Lambda S3 event notifications.
description:
    - This module allows the management of AWS Lambda function event source mappings such as S3 bucket
      events, DynamoDB and Kinesis streaming events via the Ansible framework.
      It is idempotent and supports "Check" mode.  Use module M(lambda) to manage the lambda
      function itself and M(lambda_alias) to manage function aliases.
version_added: "2.1"
author: Pierre Jodouin (@pjodouin)
options:
  lambda_function_arn:
    description:
      - The name or ARN of the lambda function.
    required: true
    aliases: ['function_name', 'function_arn']
  state:
    description:
      - Describes the desired state and defaults to "present".
    required: true
    default: "present"
    choices: ["present", "absent"]
  alias:
    description:
      - Name of the function alias. Mutually exclusive with C(version).
    required: true
  version:
    description:
      -  Version of the Lambda function. Mutually exclusive with C(alias).
    required: false
  event_source:
    description:
      -  Source of the event that triggers the lambda function.
    required: true
    choices: ['s3', 'Kinesis', 'DynamoDB', 'SNS']
  source_params:
    description:
      -  Sub-parameters required for event source.
      -  I(== S3 event source ==)
      -  C(id) Unique ID for this source event.
      -  C(bucket) Name of source bucket.
      -  C(prefix) Bucket prefix (e.g. images/)
      -  C(suffix) Bucket suffix (e.g. log)
      -  C(events) List of events (e.g. ['s3:ObjectCreated:Put'])
    required: true
requirements:
    - boto3
extends_documentation_fragment:
    - aws

'''

EXAMPLES = '''
---
# Simple example that creates a lambda event notification for an S3 bucket
- hosts: localhost
  gather_facts: no
  vars:
    state: present
  tasks:
  - name: S3 event mapping
    lambda_event:
      state: "{{ state | default('present') }}"
      event_source: s3
      function_name: ingestData
      alias: Dev
      source_params:
        id: lambda-s3-myBucket-create-data-log
        bucket: buzz-scanner
        prefix: twitter
        suffix: log
        events:
        - s3:ObjectCreated:Put

'''

RETURN = '''
---
lambda_s3_events:
    description: list of dictionaries returned by the API describing S3 event mappings
    returned: success
    type: list

lambda_sns_event:
    description: dictionary returned by the API describing SNS event mapping
    returned: success
    type: dict

'''

# ---------------------------------------------------------------------------------------------------
#
#   Helper Functions & classes
#
# ---------------------------------------------------------------------------------------------------


class AWSConnection:
    """
    Create the connection object and client objects as required.
    """

    def __init__(self, ansible_obj, resources, boto3=True):

        try:
            self.region, self.endpoint, aws_connect_kwargs = get_aws_connection_info(ansible_obj, boto3=boto3)

            self.resource_client = dict()
            if not resources:
                resources = ['lambda']

            resources.append('iam')

            for resource in resources:
                aws_connect_kwargs.update(dict(region=self.region,
                                               endpoint=self.endpoint,
                                               conn_type='client',
                                               resource=resource
                                               ))
                self.resource_client[resource] = boto3_conn(ansible_obj, **aws_connect_kwargs)

            # if region is not provided, then get default profile/session region
            if not self.region:
                self.region = self.resource_client['lambda'].meta.region_name

        except (ClientError, ParamValidationError, MissingParametersError) as e:
            ansible_obj.fail_json(msg="Unable to connect, authorize or access resource: {0}".format(e))

        # set account ID
        try:
            self.account_id = self.resource_client['iam'].get_user()['User']['Arn'].split(':')[4]
        except (ClientError, ValueError, KeyError, IndexError):
            self.account_id = ''

    def client(self, resource='lambda'):
        return self.resource_client[resource]


def pc(key):
    """
    Changes python key into Pascale case equivalent. For example, 'this_function_name' becomes 'ThisFunctionName'.

    :param key:
    :return:
    """

    return "".join([token.capitalize() for token in key.split('_')])


def ordered_obj(obj):
    """
    Order object for comparison purposes

    :param obj:
    :return:
    """

    if isinstance(obj, dict):
        return sorted((k, ordered_obj(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return sorted(ordered_obj(x) for x in obj)
    else:
        return obj


def set_api_sub_params(params):
    """
    Sets module sub-parameters to those expected by the boto3 API.

    :param module_params:
    :return:
    """

    api_params = dict()

    for param in params.keys():
        param_value = params.get(param, None)
        if param_value:
            api_params[pc(param)] = param_value

    return api_params


def validate_params(module, aws):
    """
    Performs basic parameter validation.

    :param module:
    :param aws:
    :return:
    """

    # function_name = module.params['lambda_function_arn']
    #
    # # validate function name
    # if not re.search('^[\w\-:]+$', function_name):
    #     module.fail_json(
    #             msg='Function name {0} is invalid. Names must contain only alphanumeric characters and hyphens.'.format(function_name)
    #     )
    # if len(function_name) > 64:
    #     module.fail_json(msg='Function name "{0}" exceeds 64 character limit'.format(function_name))
    #
    # # check if 'function_name' needs to be expanded in full ARN format
    # if not module.params['lambda_function_arn'].startswith('arn:aws:lambda:'):
    #     function_name = module.params['lambda_function_arn']
    #     module.params['lambda_function_arn'] = 'arn:aws:lambda:{0}:{1}:function:{2}'.format(aws.region, aws.account_id, function_name)
    #
    # qualifier = get_qualifier(module)
    # if qualifier:
    #     function_arn = module.params['lambda_function_arn']
    #     module.params['lambda_function_arn'] = '{0}:{1}'.format(function_arn, qualifier)
    #
    return


def get_qualifier(module):
    """
    Returns the function qualifier as a version or alias or None.

    :param module:
    :return:
    """

    qualifier = None
    if module.params['version'] > 0:
        qualifier = str(module.params['version'])
    elif module.params['alias']:
        qualifier = str(module.params['alias'])

    return qualifier


def assert_policy_state(module, aws, policy, present=False):
    """
    Asserts the desired policy statement is present/absent and adds/removes it accordingly.

    :param module:
    :param aws:
    :param policy:
    :param present:
    :return:
    """

    changed = False
    currently_present = get_policy_state(module, aws, policy['statement_id'])

    if present:
        if not currently_present:
            changed = add_policy_permission(module, aws, policy)
    else:
        if currently_present:
            changed = remove_policy_permission(module, aws, policy['statement_id'])

    return changed


def get_policy_state(module, aws, sid):
    """
    Checks that policy exists and if so, that statement ID is present or absent.

    :param module:
    :param aws:
    :param sid:
    :return:
    """

    client = aws.client('lambda')
    policy = dict()
    present = False

    # set API parameters
    api_params = dict(FunctionName=module.params['lambda_function_arn'])
    qualifier = get_qualifier(module)
    if qualifier:
        api_params.update(Qualifier=qualifier)

    # check if function policy exists
    try:
        # get_policy returns a JSON string so must convert to dict before reassigning to its key
        policy_results = client.get_policy(**api_params)
        policy = json.loads(policy_results.get('Policy', '{}'))

    except (ClientError, ParamValidationError, MissingParametersError) as e:
        if not e.response['Error']['Code'] == 'ResourceNotFoundException':
            module.fail_json(msg='Error retrieving function policy: {0}'.format(e))

    if 'Statement' in policy:
        # now that we have the policy, check if required permission statement is present
        for statement in policy['Statement']:
            if statement['Sid'] == sid:
                present = True
                break

    return present


def add_policy_permission(module, aws, policy_statement):
    """
    Adds a permission statement to the policy.

    :param module:
    :param aws:
    :param policy_statement:
    :return:
    """

    client = aws.client('lambda')
    changed = False

    # set API parameters
    api_params = dict(FunctionName=module.params['lambda_function_arn'])
    api_params.update(set_api_sub_params(policy_statement))
    qualifier = get_qualifier(module)
    if qualifier:
        api_params.update(Qualifier=qualifier)

    try:
        if not module.check_mode:
            client.add_permission(**api_params)
        changed = True
    except (ClientError, ParamValidationError, MissingParametersError) as e:
        module.fail_json(msg='Error adding permission to policy: {0}'.format(e))

    return changed


def remove_policy_permission(module, aws, statement_id):
    """
    Removed a permission statement from the policy.

    :param module:
    :param aws:
    :param statement_id:
    :return:
    """

    client = aws.client('lambda')
    changed = False

    # set API parameters
    api_params = dict(FunctionName=module.params['lambda_function_arn'])
    api_params.update(StatementId=statement_id)
    qualifier = get_qualifier(module)
    if qualifier:
        api_params.update(Qualifier=qualifier)

    try:
        if not module.check_mode:
            client.remove_permission(**api_params)
        changed = True
    except (ClientError, ParamValidationError, MissingParametersError) as e:
        module.fail_json(msg='Error removing permission from policy: {0}'.format(e))

    return changed


def get_arn(module):

    service_configs = {
        'topic': 'TopicConfigurations',
        'queue': 'QueueConfigurations',
        'lambda': 'LambdaFunctionConfigurations'
        }

    service_arn = None
    service = None

    for item in ('topic_arn', 'queue_arn', 'lambda_function_arn'):
        if module.params[item]:
            service_arn = module.params[item]
            service = item.split('_', 1)[0]
            break

    if not service_arn:
        module.fail_json(msg='Error: exactly one target service ARN is required.')

    return service_configs[service], service_arn


# ---------------------------------------------------------------------------------------------------
#
#   S3 Event Handlers
#
# ---------------------------------------------------------------------------------------------------

def s3_event_notification(module, aws):
    """
    Adds, updates or deletes s3 event notifications.

    :param module: Ansible module reference
    :param aws:
    :return dict:
    """

    client = aws.client('s3')
    api_params = dict()
    changed = False
    current_state = 'absent'
    state = module.params['state']

    # check if required sub-parameters are present
    source_params = module.params['source_params']
    if not source_params.get('id'):
        module.fail_json(msg="Source parameter 'id' is required for S3 event notification.")

    if source_params.get('bucket'):
        api_params = dict(Bucket=source_params['bucket'])
    else:
        module.fail_json(msg="Source parameter 'bucket' is required for S3 event notification.")

    # check if event notifications exist
    try:
        facts = client.get_bucket_notification_configuration(**api_params)
        facts.pop('ResponseMetadata')
    except ClientError as e:
        module.fail_json(msg='Error retrieving s3 event notification configuration: {0}'.format(e))


    configurations, service_arn = get_arn(module)

    current_lambda_configs = list()
    matching_id_config = dict()


    if 'LambdaFunctionConfigurations' in facts:
        current_lambda_configs = facts.pop('LambdaFunctionConfigurations')

        for config in current_lambda_configs:
            if config['Id'] == source_params['id']:
                matching_id_config = config
                current_lambda_configs.remove(config)
                current_state = 'present'
                break

    if state == 'present':
        # build configurations
        new_configuration = dict(Id=source_params.get('id'))
        new_configuration.update(LambdaFunctionArn=module.params['lambda_function_arn'])

        filter_rules = []
        if source_params.get('prefix'):
            filter_rules.append(dict(Name='Prefix', Value=str(source_params.get('prefix'))))
        if source_params.get('suffix'):
            filter_rules.append(dict(Name='Suffix', Value=str(source_params.get('suffix'))))
        if filter_rules:
            new_configuration.update(Filter=dict(Key=dict(FilterRules=filter_rules)))
        if source_params.get('events'):
            new_configuration.update(Events=source_params['events'])

        if current_state == 'present':

            # check if source event configuration has changed
            if ordered_obj(matching_id_config) == ordered_obj(new_configuration):
                current_lambda_configs.append(matching_id_config)
            else:
                # update s3 event notification for lambda
                current_lambda_configs.append(new_configuration)
                facts.update(LambdaFunctionConfigurations=current_lambda_configs)
                api_params = dict(NotificationConfiguration=facts, Bucket=source_params['bucket'])

                try:
                    if not module.check_mode:
                        client.put_bucket_notification_configuration(**api_params)
                    changed = True
                except (ClientError, ParamValidationError, MissingParametersError) as e:
                    module.fail_json(msg='Error updating s3 event notification for lambda: {0}'.format(e))

        else:
            # add policy permission before creating the event notification
            policy = dict(
                statement_id=source_params['id'],
                action='lambda:InvokeFunction',
                principal='s3.amazonaws.com',
                source_arn='arn:aws:s3:::{0}'.format(source_params['bucket']),
                source_account=aws.account_id,
            )
            assert_policy_state(module, aws, policy, present=True)

            # create s3 event notification for lambda
            current_lambda_configs.append(new_configuration)
            facts.update(LambdaFunctionConfigurations=current_lambda_configs)
            api_params = dict(NotificationConfiguration=facts, Bucket=source_params['bucket'])

            try:
                if not module.check_mode:
                    client.put_bucket_notification_configuration(**api_params)
                changed = True
            except (ClientError, ParamValidationError, MissingParametersError) as e:
                module.fail_json(msg='Error creating s3 event notification for lambda: {0}'.format(e))

    else:
        # state = 'absent'
        if current_state == 'present':

            # delete the lambda event notifications
            if current_lambda_configs:
                facts.update(LambdaFunctionConfigurations=current_lambda_configs)

            api_params.update(NotificationConfiguration=facts)

            try:
                if not module.check_mode:
                    client.put_bucket_notification_configuration(**api_params)
                changed = True
            except (ClientError, ParamValidationError, MissingParametersError) as e:
                module.fail_json(msg='Error removing s3 source event configuration: {0}'.format(e))

            policy = dict(
                statement_id=source_params['id'],
            )
            assert_policy_state(module, aws, policy, present=False)

    return dict(changed=changed, ansible_facts=dict(lambda_s3_events=current_lambda_configs))


# ---------------------------------------------------------------------------------------------------
#
#   MAIN
#
# ---------------------------------------------------------------------------------------------------

def main():
    """
    Main entry point.

    :return dict: ansible facts
    """

    # produce a list of function suffixes which handle lambda events.
    this_module = sys.modules[__name__]
    source_choices = [function.split('_')[-1] for function in dir(this_module) if function.startswith('lambda_event')]

    argument_spec = ec2_argument_spec()
    argument_spec.update(dict(
        state=dict(required=False, default='present', choices=['present', 'absent']),
        bucket=dict(required=True, default=None, aliases=['bucket_name', ]),
        prefix=dict(required=False, default=None),
        suffix=dict(required=False, default=None),
        topic_arn=dict(required=False, default=None, aliases=['topic', ]),
        queue_arn=dict(required=False, default=None, aliases=['queue', ]),
        lambda_function_arn=dict(required=False, default=None, aliases=['function_arn', 'lambda_arn']),
        events=dict(type='list', required=True, default=None)
        )
    )

    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
        mutually_exclusive=[['topic_arn', 'queue_arn', 'lambda_function_arn']],
        required_together=[]
    )

    # validate dependencies
    if not HAS_BOTO3:
        module.fail_json(msg='Both boto3 & boto are required for this module.')

    aws = AWSConnection(module, ['lambda', 's3', 'sns'])

    # validate_params(module, aws)

    this_module_function = getattr(this_module, 'lambda_event_{}'.format(module.params['event_source'].lower()))

    results = this_module_function(module, aws)

    module.exit_json(**results)


# ansible import module(s) kept at ~eof as recommended
from ansible.module_utils.basic import *
from ansible.module_utils.ec2 import *

if __name__ == '__main__':
    main()