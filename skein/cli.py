from __future__ import print_function, division, absolute_import

import argparse
import os
import sys
import traceback

import yaml

from . import __version__
from .core import Client, ApplicationClient, Security
from .exceptions import context, SkeinError, DaemonNotRunningError
from .model import ApplicationSpec, ContainerState, ApplicationState
from .utils import format_table, humanize_timedelta


class _Formatter(argparse.HelpFormatter):
    """Format with a fixed argument width, due to bug in argparse measuring
    argument widths"""
    @property
    def _action_max_length(self):
        return 16

    @_action_max_length.setter
    def _action_max_length(self, value):
        pass


class _VersionAction(argparse.Action):
    def __init__(self, option_strings, version=None, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help="Show version then exit"):
        super(_VersionAction, self).__init__(option_strings=option_strings,
                                             dest=dest, default=default,
                                             nargs=0, help=help)
        self.version = version

    def __call__(self, parser, namespace, values, option_string=None):
        print(self.version % {'prog': parser.prog})
        sys.exit(0)


def fail(msg, prefix=True):
    if prefix:
        msg = 'Error: %s' % msg
    print(msg, file=sys.stderr)
    sys.exit(1)


def add_help(parser):
    parser.add_argument("--help", "-h", action='help',
                        help="Show this help message then exit")


def arg(*args, **kwargs):
    return (args, kwargs)


def subcommand(subparsers, name, help, *args):
    def _(func):
        parser = subparsers.add_parser(name,
                                       help=help,
                                       formatter_class=_Formatter,
                                       description=help,
                                       add_help=False)
        parser.set_defaults(func=func)
        for arg in args:
            parser.add_argument(*arg[0], **arg[1])
        add_help(parser)
        func.parser = parser
        return func
    return _


def node(subs, name, help):
    @subcommand(subs, name, help)
    def f():
        fail(f.parser.format_usage(), prefix=False)
    f.subs = f.parser.add_subparsers(metavar='command', dest='command')
    f.subs.required = True
    return f


entry = argparse.ArgumentParser(prog="skein",
                                description="Define and run YARN applications",
                                formatter_class=_Formatter,
                                add_help=False)
add_help(entry)
entry.add_argument("--version", action=_VersionAction,
                   version='%(prog)s ' + __version__,
                   help="Show version then exit")
entry.set_defaults(func=lambda: fail(entry.format_usage(), prefix=False))
entry_subs = entry.add_subparsers(metavar='command', dest='command')
entry_subs.required = True

# Common arguments
app_id = arg('app_id', help='The application id', metavar='APP_ID')
app_id_or_current = arg('app_id', metavar='APP_ID',
                        help='The application id. To use in a container inside '
                             'a skein application, pass in "current"')
container_id = arg('--id', required=True,
                   help='The container id', metavar='CONTAINER_ID')


def application_client_from_app_id(app_id):
    if app_id == 'current':
        return ApplicationClient.from_current()
    return Client.from_global_daemon().connect(app_id)


###################
# DAEMON COMMANDS #
###################

daemon = node(entry_subs, 'daemon', 'Manage the skein daemon')

log = arg("--log", default=False,
          help="If provided, the daemon will write logs here.")


@subcommand(daemon.subs,
            'start', 'Start the skein daemon',
            log)
def daemon_start(log=False):
    print(Client.start_global_daemon(log=log))


@subcommand(daemon.subs,
            'address', 'The address of the running daemon')
def daemon_address():
    try:
        client = Client.from_global_daemon()
        print(client.address)
    except DaemonNotRunningError:
        fail("No skein daemon is running")


@subcommand(daemon.subs,
            'stop', 'Stop the skein daemon')
def daemon_stop():
    Client.stop_global_daemon()


@subcommand(daemon.subs,
            'restart', 'Restart the skein daemon',
            log)
def daemon_restart(log=False):
    daemon_stop()
    daemon_start(log=log)


#####################
# KEYSTORE COMMANDS #
#####################

kv = node(entry_subs, 'kv', 'Manage the skein key-value store')


@subcommand(kv.subs,
            'get', 'Get a value from the key-value store',
            app_id_or_current,
            arg('--key', help='The key to get. Omit to get the whole '
                              'key-value store.'),
            arg('--wait', action='store_true',
                help='If true, will block until the key is set'))
def kv_get(app_id, key=None, wait=False):
    app = application_client_from_app_id(app_id)

    if key is None:
        result = app.kv.to_dict()
        if result:
            print('\n'.join('%s: %s' % i for i in sorted(result.items())))
    else:
        result = app.kv.wait(key) if wait else app.kv[key]
        print(result)


@subcommand(kv.subs,
            'set', 'Set a value in the key-value store',
            app_id_or_current,
            arg('--key', required=True, help='The key to set'),
            arg('--value', required=True, help='The value to set'))
def kv_set(app_id, key, value):
    app = application_client_from_app_id(app_id)
    app.kv[key] = value


@subcommand(kv.subs,
            'del', 'Delete a value from the key-value store',
            app_id_or_current,
            arg('--key', required=True, help='The key to delete.'))
def kv_del(app_id, key):
    app = application_client_from_app_id(app_id)
    del app.kv[key]


########################
# APPLICATION COMMANDS #
########################

application = node(entry_subs, 'application', 'Manage applications')


def _print_application_status(apps):
    header = ['application_id', 'name', 'state', 'status', 'containers',
              'vcores', 'memory', 'runtime']
    data = [(a.id, a.name, a.state, a.final_status,
             a.usage.num_used_containers,
             a.usage.used_resources.vcores,
             a.usage.used_resources.memory,
             humanize_timedelta(a.runtime))
            for a in apps]
    print(format_table(header, data))


@subcommand(application.subs,
            'submit', 'Submit a Skein Application',
            arg('spec', help='The specification file'))
def application_submit(spec):
    if not os.path.exists(spec):
        fail("No application specification file at %r" % spec)
    try:
        spec = ApplicationSpec.from_file(spec)
    except SkeinError as exc:
        # Prettify expected errors, let rest bubble up
        fail('In file %r, %s' % (spec, exc))

    app = Client.from_global_daemon().submit(spec)
    print(app.app_id)


@subcommand(application.subs,
            'ls', 'List applications',
            arg('--all', '-a', action='store_true',
                help=('Show all applications (default is only active '
                      'applications)')),
            arg("--state", "-s", action='append',
                help=('Filter by application states. May be repeated '
                      'to select multiple states.')))
def application_ls(all=False, state=None):
    if all and state is None:
        state = tuple(ApplicationState)
    apps = Client.from_global_daemon().applications(states=state)
    _print_application_status(apps)


@subcommand(application.subs,
            'status', 'Status of a Skein application',
            app_id)
def application_status(app_id):
    apps = Client.from_global_daemon().status(app_id)
    _print_application_status([apps])


@subcommand(application.subs,
            'kill', 'Kill a Skein application',
            app_id)
def application_kill(app_id):
    Client.from_global_daemon().kill(app_id)


@subcommand(application.subs,
            'shutdown', 'Shutdown a Skein application',
            app_id_or_current,
            arg('--status', default='SUCCEEDED',
                help='Final Application Status. Default is SUCCEEDED'))
def application_shutdown(app_id, status):
    application_client_from_app_id(app_id).shutdown(status)


@subcommand(application.subs,
            'describe', 'Get specifications for a running skein application',
            app_id,
            arg('--service', '-s', help='Service name'))
def application_describe(app_id, service=None):
    client = Client.from_global_daemon()
    resp = client.connect(app_id).describe(service=service)
    if service is not None:
        out = yaml.safe_dump({service: resp.to_dict(skip_nulls=True)},
                             default_flow_style=False)
    else:
        out = resp.to_yaml(skip_nulls=True)
    print(out)


######################
# CONTAINER COMMANDS #
######################

container = node(entry_subs, 'container', 'Manage containers')


def _print_container_status(containers):
    header = ['service', 'id', 'state', 'runtime']
    data = [(c.service_name, c.id, c.state, humanize_timedelta(c.runtime))
            for c in containers]
    print(format_table(header, data))


@subcommand(container.subs,
            'ls', 'List containers',
            app_id_or_current,
            arg('--all', '-a', action='store_true',
                help='Show all containers (default is only active containers)'),
            arg("--service", action='append',
                help=('Filter by container services. May be repeated '
                      'to select multiple services.')),
            arg("--state", action='append',
                help=('Filter by container states. May be repeated '
                      'to select multiple states.')))
def container_ls(app_id, all=False, service=None, state=None):
    app = application_client_from_app_id(app_id)
    if all and state is None:
        state = tuple(ContainerState)
    containers = app.containers(states=state, services=service)
    _print_container_status(containers)


@subcommand(container.subs,
            'kill', 'Kill a container',
            app_id_or_current,
            container_id)
def container_kill(app_id, id):
    application_client_from_app_id(app_id).kill(id)


@subcommand(container.subs,
            'scale', 'Scale a service to a requested number of containers',
            app_id_or_current,
            arg('--service', '-s', required=True, help='Service name'),
            arg('--number', '-n', type=int, required=True,
                help='The requested number of instances'))
def container_scale(app_id, service, number):
    application_client_from_app_id(app_id).scale(service, number)


################
# INIT COMMAND #
################


@subcommand(entry_subs,
            'init', 'Initialize skein configuration',
            arg('--force', '-f', action='store_true',
                help='Overwrite existing configuration'))
def entry_init(force=False):
    Security.from_new_directory(force=force)


def main(args=None):
    kwargs = vars(entry.parse_args(args=args))
    kwargs.pop('command', None)  # Drop unnecessary `command` arg
    func = kwargs.pop('func')
    try:
        with context.set_cli():
            func(**kwargs)
    except KeyError as exc:
        fail("Key %s is not set" % str(exc))
    except DaemonNotRunningError as exc:
        fail("Skein daemon not found, please run `skein daemon start`")
    except SkeinError as exc:
        fail(str(exc))
    except Exception as exc:
        fail("Unexpected Error:\n%s" % traceback.format_exc(), prefix=False)
    sys.exit(0)


if __name__ == '__main__':
    main()
