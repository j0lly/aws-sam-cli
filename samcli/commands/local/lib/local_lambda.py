"""
Implementation of Local Lambda runner
"""

import os
import logging
import boto3

from samcli.lib.utils.codeuri import resolve_code_path
from samcli.local.lambdafn.env_vars import EnvironmentVariables
from samcli.local.lambdafn.config import FunctionConfig
from samcli.local.lambdafn.exceptions import FunctionNotFound
from samcli.commands.local.lib.exceptions import OverridesNotWellDefinedError

LOG = logging.getLogger(__name__)


class LocalLambdaRunner(object):
    """
    Runs Lambda functions locally. This class is a wrapper around the `samcli.local` library which takes care
    of actually running the function on a Docker container.
    """
    MAX_DEBUG_TIMEOUT = 36000  # 10 hours in seconds

    def __init__(self,
                 local_runtime,
                 function_provider,
                 cwd,
                 env_vars_values=None,
                 debug_context=None):
        """
        Initializes the class

        :param samcli.local.lambdafn.runtime.LambdaRuntime local_runtime: Lambda runtime capable of running a function
        :param samcli.commands.local.lib.provider.FunctionProvider function_provider: Provider that can return a
            Lambda function
        :param string cwd: Current working directory. We will resolve all function CodeURIs relative to this directory.
        :param dict env_vars_values: Optional. Dictionary containing values of environment variables
        :param integer debug_port: Optional. Port to bind the debugger to
        :param string debug_args: Optional. Additional arguments passed to the debugger
        """

        self.local_runtime = local_runtime
        self.provider = function_provider
        self.cwd = cwd
        self.env_vars_values = env_vars_values or {}
        self.debug_context = debug_context

    def invoke(self, function_name, event, stdout=None, stderr=None):
        """
        Find the Lambda function with given name and invoke it. Pass the given event to the function and return
        response through the given streams.

        This function will block until either the function completes or times out.

        Parameters
        ----------
        function_name str
            Name of the Lambda function to invoke
        event str
            Event data passed to the function. Must be a valid JSON String.
        stdout samcli.lib.utils.stream_writer.StreamWriter
            Stream writer to write the output of the Lambda function to.
        stderr samcli.lib.utils.stream_writer.StreamWriter
            Stream writer to write the Lambda runtime logs to.

        Raises
        ------
        FunctionNotfound
            When we cannot find a function with the given name
        """

        # Generate the correct configuration based on given inputs
        function = self.provider.get(function_name)

        if not function:
            all_functions = [f.name for f in self.provider.get_all()]
            available_function_message = "{} not found. Possible options in your template: {}"\
                .format(function_name, all_functions)
            LOG.info(available_function_message)
            raise FunctionNotFound("Unable to find a Function with name '%s'", function_name)

        LOG.debug("Found one Lambda function with name '%s'", function_name)

        LOG.info("Invoking %s (%s)", function.handler, function.runtime)
        config = self._get_invoke_config(function)

        # Invoke the function
        with function.codeuri:
            self.local_runtime.invoke(config, event, debug_context=self.debug_context, stdout=stdout, stderr=stderr)

    def is_debugging(self):
        """
        Are we debugging the invoke?

        Returns
        -------
        bool
            True, if we are debugging the invoke ie. the Docker container will break into the debugger and wait for
            attach
        """
        return bool(self.debug_context)

    def _get_invoke_config(self, function):
        """
        Returns invoke configuration to pass to Lambda Runtime to invoke the given function

        :param samcli.commands.local.lib.provider.Function function: Lambda function to generate the configuration for
        :return samcli.local.lambdafn.config.FunctionConfig: Function configuration to pass to Lambda runtime
        """

        env_vars = self._make_env_vars(function)
        code_abs_path = resolve_code_path(self.cwd, function.codeuri)

        LOG.debug("Resolved absolute path to code is %s", code_abs_path)

        function_timeout = function.timeout

        # The Runtime container handles timeout inside the container. When debugging with short timeouts, this can
        # cause the container execution to stop. When in debug mode, we set the timeout in the container to a max 10
        # hours. This will ensure the container doesn't unexpectedly stop while debugging function code
        if self.is_debugging():
            function_timeout = self.MAX_DEBUG_TIMEOUT

        return FunctionConfig(name=function.name,
                              runtime=function.runtime,
                              handler=function.handler,
                              code_abs_path=code_abs_path,
                              layers=function.layers,
                              memory=function.memory,
                              timeout=function_timeout,
                              env_vars=env_vars)

    def _make_env_vars(self, function):
        """Returns the environment variables configuration for this function

        Parameters
        ----------
        function : samcli.commands.local.lib.provider.Function
            Lambda function to generate the configuration for

        Returns
        -------
        samcli.local.lambdafn.env_vars.EnvironmentVariables
            Environment variable configuration for this function

        Raises
        ------
        samcli.commands.local.lib.exceptions.OverridesNotWellDefinedError
            If the environment dict is in the wrong format to process environment vars

        """

        name = function.name

        variables = None
        if function.environment and isinstance(function.environment, dict) and "Variables" in function.environment:
            variables = function.environment["Variables"]
        else:
            LOG.debug("No environment variables found for function '%s'", name)

        # This could either be in standard format, or a CloudFormation parameter file format.
        #
        # Standard format is {FunctionName: {key:value}, FunctionName: {key:value}}
        # CloudFormation parameter file is {"Parameters": {key:value}}

        for env_var_value in self.env_vars_values.values():
            if not isinstance(env_var_value, dict):
                reason = """
                            Environment variables must be in either CloudFormation parameter file
                            format or in {FunctionName: {key:value}} JSON pairs
                            """
                LOG.debug(reason)
                raise OverridesNotWellDefinedError(reason)

        if "Parameters" in self.env_vars_values:
            LOG.debug("Environment variables overrides data is in CloudFormation parameter file format")
            # CloudFormation parameter file format
            overrides = self.env_vars_values["Parameters"]
        else:
            # Standard format
            LOG.debug("Environment variables overrides data is standard format")
            overrides = self.env_vars_values.get(name, None)

        shell_env = os.environ
        aws_creds = self.get_aws_creds()

        return EnvironmentVariables(function.memory,
                                    function.timeout,
                                    function.handler,
                                    variables=variables,
                                    shell_env_values=shell_env,
                                    override_values=overrides,
                                    aws_creds=aws_creds)

    def get_aws_creds(self):
        """
        Returns AWS credentials obtained from the shell environment or given profile

        :return dict: A dictionary containing credentials. This dict has the structure
             {"region": "", "key": "", "secret": "", "sessiontoken": ""}. If credentials could not be resolved,
             this returns None
        """
        result = {}

        # to pass command line arguments for region & profile to setup boto3 default session
        if boto3.DEFAULT_SESSION:
            session = boto3.DEFAULT_SESSION
        else:
            session = boto3.session.Session()

        profile_name = session.profile_name if session else None
        LOG.debug("Loading AWS credentials from session with profile '%s'", profile_name)

        if not session:
            return result

        # Load the credentials from profile/environment
        creds = session.get_credentials()

        if not creds:
            # If we were unable to load credentials, then just return empty. We will use the default
            return result

        # After loading credentials, region name might be available here.
        if hasattr(session, 'region_name') and session.region_name:
            result["region"] = session.region_name

        # Only add the key, if its value is present
        if hasattr(creds, 'access_key') and creds.access_key:
            result["key"] = creds.access_key

        if hasattr(creds, 'secret_key') and creds.secret_key:
            result["secret"] = creds.secret_key

        if hasattr(creds, 'token') and creds.token:
            result["sessiontoken"] = creds.token

        return result
