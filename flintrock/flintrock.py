import os
import posixpath
import errno
import sys
import shutil
import textwrap
import traceback
import warnings

# External modules
import click
import yaml

# Flintrock modules
from . import ec2
from .exceptions import (
    UsageError,
    UnsupportedProviderError,
    NothingToDo,
    Error)
from flintrock import __version__
from .services import HDFS, Spark  # TODO: Remove this dependency.

FROZEN = getattr(sys, 'frozen', False)

if FROZEN:
    THIS_DIR = sys._MEIPASS
else:
    THIS_DIR = os.path.dirname(os.path.realpath(__file__))


def format_message(*, message: str, indent: int=4, wrap: int=70):
    """
    Format a lengthy message for printing to screen.
    """
    return textwrap.indent(
        textwrap.fill(
            textwrap.dedent(text=message),
            width=wrap),
        prefix=' ' * indent)


def option_name_to_variable_name(option: str):
    """
    Convert an option name like `--ec2-user` to the Python name it gets mapped to,
    like `ec2_user`.
    """
    return option.replace('--', '', 1).replace('-', '_')


def variable_name_to_option_name(variable: str):
    """
    Convert a variable name like `ec2_user` to the Click option name it gets mapped to,
    like `--ec2-user`.
    """
    return '--' + variable.replace('_', '-')


def option_requires(
        *,
        option: str,
        conditional_value=None,
        requires_all: list=[],
        requires_any: list=[],
        scope: dict):
    """
    Raise an exception if an option's requirements are not met. If conditional_value
    is not None, then only check the requirements if option is set to that value.

    requires_all: Every option in this list must be defined.
    requires_any: At least one option in this list must be defined.

    This function looks for values by converting the option names to their
    corresponding variable names (e.g. --option-a becomes option_a) and looking them
    up in the provided scope.
    """
    if (conditional_value is None or
            scope[option_name_to_variable_name(option)] == conditional_value):
        if requires_all:
            for required_option in requires_all:
                required_name = option_name_to_variable_name(required_option)
                if required_name not in scope or scope[required_name] is None:
                    raise UsageError(
                        'Error: Missing option "{missing_option}" is required by '
                        '"{option}{space}{conditional_value}".'.format(
                            missing_option=required_option,
                            option=option,
                            space=' ' if conditional_value is not None else '',
                            conditional_value=conditional_value if conditional_value is not None else ''))
        if requires_any:
            for required_option in requires_any:
                required_name = option_name_to_variable_name(required_option)
                if required_name in scope and scope[required_name] is not None:
                    break
            else:
                raise UsageError(
                    'Error: "{option}{space}{conditional_value}" requires at least '
                    'one of the following options to be set: {at_least}'.format(
                        option=option,
                        space=' ' if conditional_value is not None else '',
                        conditional_value=conditional_value if conditional_value is not None else '',
                        at_least=', '.join(['"' + ra + '"' for ra in requires_any])))


def mutually_exclusive(*, options: list, scope: dict):
    """
    Raise an exception if more than one of the provided options is specified.

    This function looks for values by converting the option names to their
    corresponding variable names (e.g. --option-a becomes option_a) and looking them
    up in the provided scope.
    """
    mutually_exclusive_names = [option_name_to_variable_name(o) for o in options]

    used_options = set()
    for name, value in scope.items():
        if name in mutually_exclusive_names and scope[name]:  # is not None:
            used_options.add(name)

    if len(used_options) > 1:
        bad_option1 = used_options.pop()
        bad_option2 = used_options.pop()
        raise UsageError(
            'Error: "{option1}" and "{option2}" are mutually exclusive.\n'
            '  {option1}: {value1}\n'
            '  {option2}: {value2}'.format(
                option1=variable_name_to_option_name(bad_option1),
                value1=scope[bad_option1],
                option2=variable_name_to_option_name(bad_option2),
                value2=scope[bad_option2]))


def get_config_file() -> str:
    """
    Get the path to Flintrock's default configuration file.
    """
    config_dir = click.get_app_dir(app_name='Flintrock')
    config_file = os.path.join(config_dir, 'config.yaml')
    return config_file


@click.group()
@click.option('--config', default=get_config_file())
@click.option('--provider', default='ec2', type=click.Choice(['ec2']))
@click.version_option(version=__version__)
@click.pass_context
def cli(cli_context, config, provider):
    """
    Flintrock

    A command-line tool and library for launching Apache Spark clusters.
    """
    cli_context.obj['provider'] = provider

    if os.path.isfile(config):
        with open(config) as f:
            config_raw = yaml.safe_load(f)
            config_map = config_to_click(normalize_keys(config_raw))

        cli_context.default_map = config_map
    else:
        if config != get_config_file():
            raise FileNotFoundError(errno.ENOENT, 'No such file', config)


@cli.command()
@click.argument('cluster-name')
@click.option('--num-slaves', type=int, required=True)
@click.option('--install-hdfs/--no-install-hdfs', default=False)
@click.option('--hdfs-version')
@click.option('--install-spark/--no-install-spark', default=True)
@click.option('--spark-version',
              help="Spark release version to install.")
@click.option('--spark-git-commit',
              help="Git commit hash to build Spark from. "
                   "--spark-version and --spark-git-commit are mutually exclusive.")
@click.option('--spark-git-repository',
              help="Git repository to clone Spark from.",
              default='https://github.com/apache/spark.git',
              show_default=True)
@click.option('--assume-yes/--no-assume-yes', default=False)
@click.option('--ec2-key-name')
@click.option('--ec2-identity-file',
              type=click.Path(exists=True, dir_okay=False),
              help="Path to SSH .pem file for accessing nodes.")
@click.option('--ec2-instance-type', default='m3.medium', show_default=True)
@click.option('--ec2-region', default='us-east-1', show_default=True)
# We set some of these defaults to empty strings because of boto3's parameter validation.
# See: https://github.com/boto/boto3/issues/400
@click.option('--ec2-availability-zone', default='')
@click.option('--ec2-ami')
@click.option('--ec2-user')
@click.option('--ec2-spot-price', type=float)
@click.option('--ec2-vpc-id', default='')
@click.option('--ec2-subnet-id', default='')
@click.option('--ec2-instance-profile-name', default='')
@click.option('--ec2-placement-group', default='')
@click.option('--ec2-tenancy', default='default')
@click.option('--ec2-ebs-optimized/--no-ec2-ebs-optimized', default=False)
@click.option('--ec2-instance-initiated-shutdown-behavior', default='stop',
              type=click.Choice(['stop', 'terminate']))
@click.pass_context
def launch(
        cli_context,
        cluster_name,
        num_slaves,
        install_hdfs,
        hdfs_version,
        install_spark,
        spark_version,
        spark_git_commit,
        spark_git_repository,
        assume_yes,
        ec2_key_name,
        ec2_identity_file,
        ec2_instance_type,
        ec2_region,
        ec2_availability_zone,
        ec2_ami,
        ec2_user,
        ec2_spot_price,
        ec2_vpc_id,
        ec2_subnet_id,
        ec2_instance_profile_name,
        ec2_placement_group,
        ec2_tenancy,
        ec2_ebs_optimized,
        ec2_instance_initiated_shutdown_behavior):
    """
    Launch a new cluster.
    """
    provider = cli_context.obj['provider']
    services = []

    option_requires(
        option='--install-hdfs',
        requires_all=['--hdfs-version'],
        scope=locals())
    option_requires(
        option='--install-spark',
        requires_any=[
            '--spark-version',
            '--spark-git-commit'],
        scope=locals())
    mutually_exclusive(
        options=[
            '--spark-version',
            '--spark-git-commit'],
        scope=locals())
    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=[
            '--ec2-key-name',
            '--ec2-identity-file',
            '--ec2-instance-type',
            '--ec2-region',
            '--ec2-ami',
            '--ec2-user'],
        scope=locals())

    if install_hdfs:
        hdfs = HDFS(version=hdfs_version)
        services += [hdfs]
    if install_spark:
        if spark_version:
            spark = Spark(version=spark_version)
        elif spark_git_commit:
            print("Warning: Building Spark takes a long time. "
                  "e.g. 15-20 minutes on an m3.xlarge instance on EC2.")
            spark = Spark(git_commit=spark_git_commit,
                          git_repository=spark_git_repository)
        services += [spark]

    if provider == 'ec2':
        return ec2.launch(
            cluster_name=cluster_name,
            num_slaves=num_slaves,
            services=services,
            assume_yes=assume_yes,
            key_name=ec2_key_name,
            identity_file=ec2_identity_file,
            instance_type=ec2_instance_type,
            region=ec2_region,
            availability_zone=ec2_availability_zone,
            ami=ec2_ami,
            user=ec2_user,
            spot_price=ec2_spot_price,
            vpc_id=ec2_vpc_id,
            subnet_id=ec2_subnet_id,
            instance_profile_name=ec2_instance_profile_name,
            placement_group=ec2_placement_group,
            tenancy=ec2_tenancy,
            ebs_optimized=ec2_ebs_optimized,
            instance_initiated_shutdown_behavior=ec2_instance_initiated_shutdown_behavior)
    else:
        raise UnsupportedProviderError(provider)


@cli.command()
@click.argument('cluster-name')
@click.option('--assume-yes/--no-assume-yes', default=False)
@click.option('--ec2-region', default='us-east-1', show_default=True)
@click.pass_context
def destroy(cli_context, cluster_name, assume_yes, ec2_region):
    """
    Destroy a cluster.
    """
    provider = cli_context.obj['provider']

    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=['--ec2-region'],
        scope=locals())

    if provider == 'ec2':
        cluster = ec2.get_cluster(
            cluster_name=cluster_name,
            region=ec2_region)
    else:
        raise UnsupportedProviderError(provider)

    if not assume_yes:
        cluster.print()
        click.confirm(
            text="Are you sure you want to destroy this cluster?",
            abort=True)

    print("Destroying {c}...".format(c=cluster.name))
    cluster.destroy()


@cli.command()
@click.argument('cluster-name', required=False)
@click.option('--master-hostname-only', is_flag=True, default=False)
@click.option('--ec2-region')
@click.pass_context
def describe(
        cli_context,
        cluster_name,
        master_hostname_only,
        ec2_region):
    """
    Describe an existing cluster.

    Leave out the cluster name to find all Flintrock-managed clusters.

    The output of this command is both human- and machine-friendly. Full cluster
    descriptions are output in YAML.
    """
    provider = cli_context.obj['provider']
    search_area = ""

    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=['--ec2-region'],
        scope=locals())

    if cluster_name:
        cluster_names = [cluster_name]
    else:
        cluster_names = []

    if provider == 'ec2':
        search_area = "in region {r}".format(r=ec2_region)
        clusters = ec2.get_clusters(
            cluster_names=cluster_names,
            region=ec2_region)
    else:
        raise UnsupportedProviderError(provider)

    if cluster_name:
        cluster = clusters[0]
        if master_hostname_only:
            print(cluster.master_host)
        else:
            cluster.print()
    else:
        if master_hostname_only:
            for cluster in sorted(clusters, key=lambda x: x.name):
                print(cluster.name + ':', cluster.master_host)
        else:
            print("Found {n} cluster{s}{space}{search_area}.".format(
                n=len(clusters),
                s='' if len(clusters) == 1 else 's',
                space=' ' if search_area else '',
                search_area=search_area))
            if clusters:
                print('---')
                for cluster in sorted(clusters, key=lambda x: x.name):
                    cluster.print()


# TODO: Provide different command or option for going straight to Spark Shell. (?)
@cli.command()
@click.argument('cluster-name')
@click.option('--ec2-region', default='us-east-1', show_default=True)
# TODO: Move identity-file to global, non-provider-specific option. (?)
@click.option('--ec2-identity-file',
              type=click.Path(exists=True, dir_okay=False),
              help="Path to SSH .pem file for accessing nodes.")
@click.option('--ec2-user')
@click.pass_context
def login(cli_context, cluster_name, ec2_region, ec2_identity_file, ec2_user):
    """
    Login to the master of an existing cluster.
    """
    provider = cli_context.obj['provider']

    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=[
            '--ec2-region',
            '--ec2-identity-file',
            '--ec2-user'],
        scope=locals())

    if provider == 'ec2':
        cluster = ec2.get_cluster(
            cluster_name=cluster_name,
            region=ec2_region)
        user = ec2_user
        identity_file = ec2_identity_file
    else:
        raise UnsupportedProviderError(provider)

    # TODO: Check that master up first and error out cleanly if not
    #       via ClusterInvalidState.
    cluster.login(user=user, identity_file=identity_file)


@cli.command()
@click.argument('cluster-name')
@click.option('--ec2-region', default='us-east-1', show_default=True)
# TODO: Move identity-file to global, non-provider-specific option. (?)
@click.option('--ec2-identity-file',
              type=click.Path(exists=True, dir_okay=False),
              help="Path to SSH .pem file for accessing nodes.")
@click.option('--ec2-user')
@click.pass_context
def start(cli_context, cluster_name, ec2_region, ec2_identity_file, ec2_user):
    """
    Start an existing, stopped cluster.
    """
    provider = cli_context.obj['provider']

    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=[
            '--ec2-region',
            '--ec2-identity-file',
            '--ec2-user'],
        scope=locals())

    if provider == 'ec2':
        cluster = ec2.get_cluster(
            cluster_name=cluster_name,
            region=ec2_region)
        user = ec2_user
        identity_file = ec2_identity_file
    else:
        raise UnsupportedProviderError(provider)

    cluster.start_check()
    print("Starting {c}...".format(c=cluster_name))
    cluster.start(user=user, identity_file=identity_file)


@cli.command()
@click.argument('cluster-name')
@click.option('--ec2-region', default='us-east-1', show_default=True)
@click.option('--assume-yes/--no-assume-yes', default=False)
@click.pass_context
def stop(cli_context, cluster_name, ec2_region, assume_yes):
    """
    Stop an existing, running cluster.
    """
    provider = cli_context.obj['provider']

    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=['--ec2-region'],
        scope=locals())

    if provider == 'ec2':
        cluster = ec2.get_cluster(
            cluster_name=cluster_name,
            region=ec2_region)
    else:
        raise UnsupportedProviderError(provider)

    cluster.stop_check()

    if not assume_yes:
        cluster.print()
        click.confirm(
            text="Are you sure you want to stop this cluster?",
            abort=True)

    print("Stopping {c}...".format(c=cluster_name))
    cluster.stop()
    print("{c} is now stopped.".format(c=cluster_name))


@cli.command(name='run-command')
@click.argument('cluster-name')
@click.argument('command', nargs=-1)
@click.option('--master-only', help="Run on the master only.", is_flag=True)
@click.option('--ec2-region', default='us-east-1', show_default=True)
@click.option('--ec2-identity-file',
              type=click.Path(exists=True, dir_okay=False),
              help="Path to SSH .pem file for accessing nodes.")
@click.option('--ec2-user')
@click.pass_context
def run_command(
        cli_context,
        cluster_name,
        command,
        master_only,
        ec2_region,
        ec2_identity_file,
        ec2_user):
    """
    Run a shell command on a cluster.

    Examples:

        flintrock run-command my-cluster 'touch /tmp/flintrock'
        flintrock run-command my-cluster -- yum install -y package

    Flintrock will return a non-zero code if any of the cluster nodes raises an error
    while running the command.
    """
    provider = cli_context.obj['provider']

    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=[
            '--ec2-region',
            '--ec2-identity-file',
            '--ec2-user'],
        scope=locals())

    if provider == 'ec2':
        cluster = ec2.get_cluster(
            cluster_name=cluster_name,
            region=ec2_region)
        user = ec2_user
        identity_file = ec2_identity_file
    else:
        raise UnsupportedProviderError(provider)

    cluster.run_command_check()

    print("Running command on {target}...".format(
        target="master only" if master_only else "cluster"))

    cluster.run_command(
        command=command,
        master_only=master_only,
        user=user,
        identity_file=identity_file)


@cli.command(name='copy-file')
@click.argument('cluster-name')
@click.argument('local_path', type=click.Path(exists=True, dir_okay=False))
@click.argument('remote_path', type=click.Path())
@click.option('--master-only', help="Copy to the master only.", is_flag=True)
@click.option('--ec2-region', default='us-east-1', show_default=True)
@click.option('--ec2-identity-file',
              type=click.Path(exists=True, dir_okay=False),
              help="Path to SSH .pem file for accessing nodes.")
@click.option('--ec2-user')
@click.option('--assume-yes/--no-assume-yes', default=False, help="Prompt before large uploads.")
@click.pass_context
def copy_file(
        cli_context,
        cluster_name,
        local_path,
        remote_path,
        master_only,
        ec2_region,
        ec2_identity_file,
        ec2_user,
        assume_yes):
    """
    Copy a local file up to a cluster.

    This will copy the file to the same path on each node of the cluster.

    Examples:

        flintrock copy-file my-cluster /tmp/file.102.txt /tmp/file.txt
        flintrock copy-file my-cluster /tmp/spark-defaults.conf /tmp/

    Flintrock will return a non-zero code if any of the cluster nodes raises an error.
    """
    provider = cli_context.obj['provider']

    option_requires(
        option='--provider',
        conditional_value='ec2',
        requires_all=[
            '--ec2-region',
            '--ec2-identity-file',
            '--ec2-user'],
        scope=locals())

    # We assume POSIX for the remote path since Flintrock
    # only supports clusters running CentOS / Amazon Linux.
    if not posixpath.basename(remote_path):
        remote_path = posixpath.join(remote_path, os.path.basename(local_path))

    if provider == 'ec2':
        cluster = ec2.get_cluster(
            cluster_name=cluster_name,
            region=ec2_region)
        user = ec2_user
        identity_file = ec2_identity_file
    else:
        raise UnsupportedProviderError(provider)

    cluster.copy_file_check()

    if not assume_yes and not master_only:
        file_size_bytes = os.path.getsize(local_path)
        num_nodes = len(cluster.slave_ips) + 1  # TODO: cluster.num_nodes
        total_size_bytes = file_size_bytes * num_nodes

        if total_size_bytes > 10 ** 6:
            print("WARNING:")
            print(
                format_message(
                    message="""\
                        You are trying to upload {total_size} bytes ({size} bytes x {count}
                        nodes in {cluster}). Depending on your upload bandwidth, this may take
                        a long time.
                        You may be better off uploading this file to a storage service like
                        Amazon S3 and downloading it from there to the cluster using
                        `flintrock run-command ...`.
                        """.format(
                            size=file_size_bytes,
                            count=num_nodes,
                            cluster=cluster_name,
                            total_size=total_size_bytes),
                    wrap=60))
            click.confirm(
                text="Are you sure you want to continue?",
                default=True,
                abort=True)

    print("Copying file to {target}...".format(
        target="master only" if master_only else "cluster"))

    cluster.copy_file(
        local_path=local_path,
        remote_path=remote_path,
        master_only=master_only,
        user=user,
        identity_file=identity_file)


def normalize_keys(obj):
    """
    Used to map keys from config files to Python parameter names.
    """
    if type(obj) != dict:
        return obj
    else:
        return {k.replace('-', '_'): normalize_keys(v) for k, v in obj.items()}


def config_to_click(config: dict) -> dict:
    """
    Convert a dictionary of configurations loaded from a Flintrock config file
    to a dictionary that Click can use to set default options.
    """
    service_configs = {}

    if 'modules' in config:
        print(
            "WARNING: The name `modules` is deprecated and will be removed "
            "in the next version of Flintrock.\n"
            "Please update your config file to use `services` instead of `modules`.\n"
            "You can do this by calling `flintrock configure`.")
        config['services'] = config['modules']

    if 'services' in config:
        for service in config['services']:
            if config['services'][service]:
                service_configs.update(
                    {service + '_' + k: v for (k, v) in config['services'][service].items()})

    ec2_configs = {
        'ec2_' + k: v for (k, v) in config['providers']['ec2'].items()}

    click_map = {
        'launch': dict(
            list(config['launch'].items()) +
            list(ec2_configs.items()) +
            list(service_configs.items())),
        'describe': ec2_configs,
        'destroy': ec2_configs,
        'login': ec2_configs,
        'start': ec2_configs,
        'stop': ec2_configs,
        'run-command': ec2_configs,
        'copy-file': ec2_configs,
    }

    return click_map


@cli.command()
@click.option('--locate', is_flag=True, default=False,
              help="Don't open an editor. "
              "Just open the folder containing the configuration file.")
@click.pass_context
def configure(cli_context, locate):
    """
    Configure Flintrock's defaults.

    This will open Flintrock's configuration file in your default YAML editor so
    you can set your defaults.
    """
    config_file = get_config_file()

    if not os.path.isfile(config_file):
        print("Initializing config file from template...")
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        shutil.copyfile(
            src=os.path.join(THIS_DIR, 'config.yaml.template'),
            dst=config_file)
        os.chmod(config_file, mode=0o644)

    click.launch(config_file, locate=locate)


def flintrock_is_in_development_mode() -> bool:
    """
    Check if Flintrock was installed in development mode.

    Use this function to toggle behavior that only Flintrock developers should
    see.
    """
    # This esoteric technique was pulled from pip.
    # See: https://github.com/pypa/pip/pull/3258/files#diff-ab583908279e865537dec218246edcfcR310
    for path_item in sys.path:
        egg_link = os.path.join(path_item, 'Flintrock.egg-link')
        if os.path.isfile(egg_link):
            return True
    else:
        return False


def main() -> int:
    if flintrock_is_in_development_mode():
        warnings.simplefilter(action='error', category=DeprecationWarning)

    try:
        # We pass in obj so we can add attributes to it, like provider, which
        # get shared by all commands.
        # See: http://click.pocoo.org/6/api/#click.Context
        cli(obj={})
    except NothingToDo as e:
        print(e)
        return 0
    except UsageError as e:
        print(e, file=sys.stderr)
        return 2
    except Exception as e:
        if not isinstance(e, Error):
            # This not one of our custom exceptions, so print
            # a traceback to help the user debug.
            traceback.print_tb(e.__traceback__, file=sys.stderr)
        print(e, file=sys.stderr)
        return 1