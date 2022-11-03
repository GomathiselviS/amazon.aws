#!/usr/bin/python
# Copyright: Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

__metaclass__ = type


DOCUMENTATION = """
---
module: lambda_alias
version_added: 5.0.0
short_description: Creates, updates or deletes AWS Lambda function aliases
description:
  - This module allows the management of AWS Lambda functions aliases via the Ansible
    framework.  It is idempotent and supports "Check" mode.    Use module M(amazon.aws.lambda) to manage the lambda function
    itself and M(amazon.aws.lambda_event) to manage event source mappings.
  - This module was originally added to C(community.aws) in release 1.0.0.

author:
  - Pierre Jodouin (@pjodouin)
  - Ryan Scott Brown (@ryansb)
options:
  function_name:
    description:
      - The name of the function alias.
    required: true
    type: str
  state:
    description:
      - Describes the desired state.
    default: "present"
    choices: ["present", "absent"]
    type: str
  name:
    description:
      - Name of the function alias.
    required: true
    aliases: ['alias_name']
    type: str
  description:
    description:
      - A short, user-defined function alias description.
    type: str
  function_version:
    description:
      -  Version associated with the Lambda function alias.
         A value of 0 (or omitted parameter) sets the alias to the $LATEST version.
    aliases: ['version']
    type: int
    default: 0
extends_documentation_fragment:
  - amazon.aws.aws
  - amazon.aws.ec2
  - amazon.aws.boto3

"""

EXAMPLES = """
---
# Simple example to create a lambda function and publish a version
- hosts: localhost
  gather_facts: false
  vars:
    state: present
    project_folder: /path/to/deployment/package
    deployment_package: lambda.zip
    account: 123456789012
    production_version: 5
  tasks:
  - name: AWS Lambda Function
    amazon.aws.lambda:
      state: "{{ state | default('present') }}"
      name: myLambdaFunction
      publish: True
      description: lambda function description
      code_s3_bucket: package-bucket
      code_s3_key: "lambda/{{ deployment_package }}"
      local_path: "{{ project_folder }}/{{ deployment_package }}"
      runtime: python2.7
      timeout: 5
      handler: lambda.handler
      memory_size: 128
      role: "arn:aws:iam::{{ account }}:role/API2LambdaExecRole"

  - name: Get information
    amazon.aws.lambda_info:
      name: myLambdaFunction
    register: lambda_info
  - name: show results
    ansible.builtin.debug:
      msg: "{{ lambda_info['lambda_facts'] }}"

# The following will set the Dev alias to the latest version ($LATEST) since version is omitted (or = 0)
  - name: "alias 'Dev' for function {{ lambda_info.lambda_facts.FunctionName }} "
    amazon.aws.lambda_alias:
      state: "{{ state | default('present') }}"
      function_name: "{{ lambda_info.lambda_facts.FunctionName }}"
      name: Dev
      description: Development is $LATEST version

# The QA alias will only be created when a new version is published (i.e. not = '$LATEST')
  - name: "alias 'QA' for function {{ lambda_info.lambda_facts.FunctionName }} "
    amazon.aws.lambda_alias:
      state: "{{ state | default('present') }}"
      function_name: "{{ lambda_info.lambda_facts.FunctionName }}"
      name: QA
      version: "{{ lambda_info.lambda_facts.Version }}"
      description: "QA is version {{ lambda_info.lambda_facts.Version }}"
    when: lambda_info.lambda_facts.Version != "$LATEST"

# The Prod alias will have a fixed version based on a variable
  - name: "alias 'Prod' for function {{ lambda_info.lambda_facts.FunctionName }} "
    amazon.aws.lambda_alias:
      state: "{{ state | default('present') }}"
      function_name: "{{ lambda_info.lambda_facts.FunctionName }}"
      name: Prod
      version: "{{ production_version }}"
      description: "Production is version {{ production_version }}"
"""

RETURN = """
---
alias_arn:
    description: Full ARN of the function, including the alias
    returned: success
    type: str
    sample: arn:aws:lambda:us-west-2:123456789012:function:myFunction:dev
description:
    description: A short description of the alias
    returned: success
    type: str
    sample: The development stage for my hot new app
function_version:
    description: The qualifier that the alias refers to
    returned: success
    type: str
    sample: $LATEST
name:
    description: The name of the alias assigned
    returned: success
    type: str
    sample: dev
revision_id:
    description: A unique identifier that changes when you update the alias.
    returned: success
    type: str
    sample: 12345678-1234-1234-1234-123456789abc
"""

import re

try:
    import botocore
except ImportError:
    pass  # Handled by AnsibleAWSModule

from ansible.module_utils.common.dict_transformations import camel_dict_to_snake_dict
from ansible.module_utils.common.dict_transformations import snake_dict_to_camel_dict

from ansible_collections.amazon.aws.plugins.module_utils.core import AnsibleAWSModule
from ansible_collections.amazon.aws.plugins.module_utils.core import is_boto3_error_code
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import AWSRetry


def set_api_params(module, module_params):
    """
    Sets non-None module parameters to those expected by the boto3 API.

    :param module:
    :param module_params:
    :return:
    """

    api_params = dict()

    for param in module_params:
        module_param = module.params.get(param, None)
        if module_param:
            api_params[param] = module_param

    return snake_dict_to_camel_dict(api_params, capitalize_first=True)


def validate_params(module):
    """
    Performs basic parameter validation.

    :param module: AnsibleAWSModule reference
    :return:
    """

    function_name = module.params["function_name"]

    # validate function name
    if not re.search(r"^[\w\-:]+$", function_name):
        module.fail_json(msg="Function name {0} is invalid. Names must contain only alphanumeric characters and hyphens.".format(function_name))
    if len(function_name) > 64:
        module.fail_json(msg='Function name "{0}" exceeds 64 character limit'.format(function_name))

    #  if parameter 'function_version' is zero, set it to $LATEST, else convert it to a string
    if module.params["function_version"] == 0:
        module.params["function_version"] = "$LATEST"
    else:
        module.params["function_version"] = str(module.params["function_version"])

    return


def get_lambda_alias(module, client):
    """
    Returns the lambda function alias if it exists.

    :param module: AnsibleAWSModule
    :param client: (wrapped) boto3 lambda client
    :return:
    """

    # set API parameters
    api_params = set_api_params(module, ("function_name", "name"))

    # check if alias exists and get facts
    try:
        results = client.get_alias(aws_retry=True, **api_params)
    except is_boto3_error_code("ResourceNotFoundException"):
        results = None
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:  # pylint: disable=duplicate-except
        module.fail_json_aws(e, msg="Error retrieving function alias")

    return results


def lambda_alias(module, client):
    """
    Adds, updates or deletes lambda function aliases.

    :param module: AnsibleAWSModule
    :param client: (wrapped) boto3 lambda client
    :return dict:
    """
    results = dict()
    changed = False
    current_state = "absent"
    state = module.params["state"]

    facts = get_lambda_alias(module, client)
    if facts:
        current_state = "present"

    if state == "present":
        if current_state == "present":
            snake_facts = camel_dict_to_snake_dict(facts)

            # check if alias has changed -- only version and description can change
            alias_params = ("function_version", "description")
            for param in alias_params:
                if module.params.get(param) is None:
                    continue
                if module.params.get(param) != snake_facts.get(param):
                    changed = True
                    break

            if changed:
                api_params = set_api_params(module, ("function_name", "name"))
                api_params.update(set_api_params(module, alias_params))

                if not module.check_mode:
                    try:
                        results = client.update_alias(aws_retry=True, **api_params)
                    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                        module.fail_json_aws(e, msg="Error updating function alias")

        else:
            # create new function alias
            api_params = set_api_params(module, ("function_name", "name", "function_version", "description"))

            try:
                if not module.check_mode:
                    results = client.create_alias(aws_retry=True, **api_params)
                changed = True
            except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                module.fail_json_aws(e, msg="Error creating function alias")

    else:  # state = 'absent'
        if current_state == "present":
            # delete the function
            api_params = set_api_params(module, ("function_name", "name"))

            try:
                if not module.check_mode:
                    results = client.delete_alias(aws_retry=True, **api_params)
                changed = True
            except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                module.fail_json_aws(e, msg="Error deleting function alias")

    return dict(changed=changed, **dict(results or facts or {}))


def main():
    """
    Main entry point.

    :return dict: ansible facts
    """
    argument_spec = dict(
        state=dict(required=False, default="present", choices=["present", "absent"]),
        function_name=dict(required=True),
        name=dict(required=True, aliases=["alias_name"]),
        function_version=dict(type="int", required=False, default=0, aliases=["version"]),
        description=dict(required=False, default=None),
    )

    module = AnsibleAWSModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
        mutually_exclusive=[],
        required_together=[],
    )

    client = module.client("lambda", retry_decorator=AWSRetry.jittered_backoff())

    validate_params(module)
    results = lambda_alias(module, client)

    module.exit_json(**camel_dict_to_snake_dict(results))


if __name__ == "__main__":
    main()
