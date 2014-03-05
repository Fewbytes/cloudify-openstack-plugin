#########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.


import copy
import inspect
import itertools
import openstack_plugin_common

from openstack_plugin_common import neutron_client
from novaclient import exceptions as nova_exceptions
from cloudify.decorators import operation

with_nova_client = openstack_plugin_common.with_nova_client

MUST_SPECIFY_NETWORK_EXCEPTION_TEXT = 'Multiple possible networks found'
SERVER_DELETE_CHECK_SLEEP = 2

NODE_ID_PROPERTY = 'cloudify_id'
OPENSTACK_SERVER_ID_PROPERTY = 'openstack_server_id'


def start_new_server(ctx, nova_client):
    """
    Creates a server. Exposes the parameters mentioned in
    http://docs.openstack.org/developer/python-novaclient/api/novaclient.v1_1
    .servers.html#novaclient.v1_1.servers.ServerManager.create
    Userdata:
        In all cases, note that userdata should not be base64 encoded,
        novaclient expects it raw.
        The 'userdata' argument under nova.instance can be one of
        the following:
        1. A string
        2. A hash with 'type: http' and 'url: ...'
    """

    # For possible changes by _maybe_transform_userdata()

    server = {
        'name': ctx.node_id
    }
    server.update(copy.deepcopy(ctx.properties['server']))

    ctx.logger.debug(
        "server.create() server before transformations: {0}".format(server))

    if server.get('nics'):
        raise ValueError("Parameter with name 'nics' must not be passed to"
                         " openstack provisioner (under host's "
                         "properties.nova.instance)")

    _maybe_transform_userdata(server)

    if ('management_network_name' in ctx.properties) and \
            ctx.properties['management_network_name']:
        nc = openstack_plugin_common.NeutronClient().get(
            config=ctx.properties.get('neutron_config'))
        managemenet_network_id = nc.cosmo_get_named(
            'network', ctx.properties['management_network_name'])['id']
        server['nics'] = [{'net-id': managemenet_network_id}]
    else:
        managemenet_network_id = None
    # print(server['nics'])

    # Sugar
    if 'image_name' in server:
        server['image'] = nova_client.images.find(name=server['image_name']).id
        del server['image_name']
    if 'flavor_name' in server:
        server['flavor'] = nova_client.flavors.find(
            name=server['flavor_name']).id
        del server['flavor_name']

    _fail_on_missing_required_parameters(
        server,
        ('name', 'flavor', 'image', 'key_name'),
        'server')

    # Multi-NIC by networks - start
    network_nodes_runtime_properties = ctx.capabilities.get_all().values()
    if network_nodes_runtime_properties and \
            'management_network_name' not in ctx.properties:
        # Known limitation
        raise RuntimeError("Nova server with multi-NIC requires "
                           "'management_network_name' which was not supplied")
    nics = [
        {'net-id': n['external_id']}
        for n in network_nodes_runtime_properties
        if neutron_client.cosmo_is_network(n['external_id'])
    ]
    if nics:
        server['nics'] = server.get('nics', []) + nics
    # Multi-NIC by networks - end

    # Multi-NIC by ports - start
    port_nodes_runtime_properties = ctx.capabilities.get_all().values()
    if port_nodes_runtime_properties and \
            'management_network_name' not in ctx.properties:
        # Known limitation
        raise RuntimeError("Nova server with multi-NIC requires "
                           "'management_network_name' which was not supplied")
    nics = [
        {'port-id': n['external_id']}
        for n in port_nodes_runtime_properties
        if neutron_client.cosmo_is_port(n['external_id'])
    ]
    if nics:
        server['nics'] = server.get('nics', []) + nics
    # Multi-NIC by ports - end

    ctx.logger.debug(
        "server.create() server after transformations: {0}".format(server))

    # First parameter is 'self', skipping
    params_names = inspect.getargspec(nova_client.servers.create).args[1:]

    params_default_values = inspect.getargspec(
        nova_client.servers.create).defaults
    params = dict(itertools.izip(params_names, params_default_values))

    # Fail on unsupported parameters
    for k in server:
        if k not in params:
            raise ValueError("Parameter with name '{0}' must not be passed to"
                             " openstack provisioner (under host's "
                             "properties.nova.instance)".format(k))

    for k in params:
        if k in server:
            params[k] = server[k]

    if not params['meta']:
        params['meta'] = dict({})
    params['meta'][NODE_ID_PROPERTY] = ctx.node_id
    if managemenet_network_id is not None:
        params['meta']['cloudify_management_network_id'] = \
            managemenet_network_id
    if 'management_network_name' in ctx.properties:
        params['meta']['cloudify_management_network_name'] = \
            ctx.properties.get('management_network_name')

    ctx.logger.info("Creating VM with parameters: {0}".format(str(params)))
    ctx.logger.debug(
        "Asking Nova to create server. All possible parameters are: {0})"
        .format(','.join(params.keys())))

    try:
        s = nova_client.servers.create(**params)
    except nova_exceptions.BadRequest as e:
        if str(e).startswith(MUST_SPECIFY_NETWORK_EXCEPTION_TEXT):
            raise RuntimeError(
                "Can not provision server: management_network_name is not "
                "specified but there are several networks that the server "
                "can be connected to."
            )
        raise RuntimeError("Nova bad request error: " + str(e))
    ctx[OPENSTACK_SERVER_ID_PROPERTY] = s.id


@operation
@with_nova_client
def start(ctx, nova_client, **kwargs):
    server = get_server_by_context(nova_client, ctx)
    if server is not None:
        server.start()
        return

    start_new_server(ctx, nova_client)


@operation
@with_nova_client
def stop(ctx, nova_client, **kwargs):
    """
    Stop server.

    Depends on OpenStack implementation, server.stop() might not be supported.
    """
    server = get_server_by_context(nova_client, ctx)
    if server is None:
        raise RuntimeError(
            "Cannot stop server - server doesn't exist for node: {0}"
            .format(ctx.node_id))
    server.stop()


@operation
@with_nova_client
def delete(ctx, nova_client, **kwargs):
    server = get_server_by_context(nova_client, ctx)
    if server is None:
        raise RuntimeError(
            "Cannot delete server - server doesn't exist for node: {0}"
            .format(ctx.node_id))

    server.delete()


def get_server_by_context(nova_client, ctx):
    """
    Gets a server for the provided context.
    
    If openstack server id is present it would be used for getting the server.
    Otherwise, an iteration on all servers metadata will be made.
    """
    # Getting server by its OpenStack id is faster tho it requires
    # a REST API call to Cloudify's storage for getting runtime properties.
    if OPENSTACK_SERVER_ID_PROPERTY in ctx:
        return nova_client.servers.get(ctx[OPENSTACK_SERVER_ID_PROPERTY])
    # Fallback
    servers = nova_client.servers.list()
    for server in servers:
        if NODE_ID_PROPERTY in server.metadata and \
                ctx.node_id == server.metadata[NODE_ID_PROPERTY]:
            return server
    return None


@operation
@with_nova_client
def get_state(ctx, nova_client, **kwargs):
    server = get_server_by_context(nova_client, ctx)
    if server.status == 'ACTIVE':
        ips = {}
        _, default_network_ips = server.networks.items()[0]
        manager_network_ip = None
        management_network_name = server.metadata.get(
            'cloudify_management_network_name')
        for network, network_ips in server.networks.items():
            if management_network_name and network == management_network_name:
                manager_network_ip = network_ips[0]
            ips[network] = network_ips
        if manager_network_ip is None:
            manager_network_ip = default_network_ips[0]
        ctx['networks'] = ips
        # The ip of this instance in the management network
        ctx['ip'] = manager_network_ip
        return True
    return False


def _fail_on_missing_required_parameters(obj, required_parameters, hint_where):
    for k in required_parameters:
        if k not in obj:
            raise ValueError(
                "Required parameter '{0}' is missing (under host's "
                "properties.{1}). Required parameters are: {2}"
                .format(k, hint_where, required_parameters))


# *** userdata handlig - start ***
userdata_handlers = {}


def userdata_handler(type_):
    def f(x):
        userdata_handlers[type_] = x
        return x
    return f


def _maybe_transform_userdata(nova_config_instance):
    """Allows userdata to be read from a file, etc, not just be a string"""
    if 'userdata' not in nova_config_instance:
        return
    if not isinstance(nova_config_instance['userdata'], dict):
        return
    ud = nova_config_instance['userdata']

    _fail_on_missing_required_parameters(
        ud,
        ('type',),
        'server.userdata')

    if ud['type'] not in userdata_handlers:
        raise ValueError("Invalid type '{0}' (under host's "
                         "properties.nova_config.instance.userdata)"
                         .format(ud['type']))

    nova_config_instance['userdata'] = userdata_handlers[ud['type']](ud)


@userdata_handler('http')
def ud_http(params):
    """ Fetches userdata using HTTP """
    import requests
    _fail_on_missing_required_parameters(
        params,
        ('url',),
        "server.userdata when using type 'http'")
    return requests.get(params['url']).text
# *** userdata handling - end ***
