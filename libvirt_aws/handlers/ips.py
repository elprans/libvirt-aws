from __future__ import annotations
from typing import (
    Any,
    Dict,
)

import itertools
import ipaddress
import uuid

import libvirt

from . import _routing
from . import errors
from .. import objects
from .. import qemu


class AddressLimitExceededError(_routing.ClientError):

    code = "AddressLimitExceeded"


class InvalidAddressID_NotFound(_routing.ClientError):

    code = "InvalidAddressID.NotFound"


class InvalidAddress_NotFound(_routing.ClientError):

    code = "InvalidAddress.NotFound"


class InvalidAssociationID_NotFound(_routing.ClientError):

    code = "InvalidAssociationID.NotFound"


@_routing.handler("DescribeAddresses")
async def describe_addresses(
    args: _routing.HandlerArgs,
    app: _routing.App,
) -> Dict[str, Any]:
    filters = args.get("Filter")
    requested_ips = args.get("PublicIp")
    tags = []
    ips = []
    instances = []
    alloc_ids = []
    assoc_ids = []
    if filters:
        for flt in filters:
            if flt["Name"].startswith("tag:"):
                tagname = flt["Name"][len("tag:")]
                tagvalue = flt["Value"]
                tags.append((tagname, tagvalue))
            elif flt["Name"] == "public-ip":
                ips.append(flt["Value"])
            elif flt["Name"] == "instance-id":
                instances.append(flt["Value"])
            elif flt["Name"] == "allocation-id":
                alloc_ids.append(flt["Value"])
            elif flt["Name"] == "association-id":
                assoc_ids.append(flt["Value"])
            else:
                raise _routing.InvalidParameterError(
                    f"unsupported filter type: {flt['Name']}")

    if requested_ips:
        requested_ips = frozenset(requested_ips) - frozenset(ips)
    elif ips:
        requested_ips = frozenset(ips)

    quals = []
    args = []
    if requested_ips:
        quals.append(
            f"ip_address IN ({','.join(('?',) * len(requested_ips))})"
        )
        args.extend(requested_ips)
    if instances:
        quals.append(
            f"instance_id IN ({','.join(('?',) * len(instances))})"
        )
        args.extend(instances)
    if alloc_ids:
        quals.append(
            f"allocation_id IN ({','.join(('?',) * len(alloc_ids))})"
        )
        args.extend(alloc_ids)
    if assoc_ids:
        quals.append(
            f"association_id IN ({','.join(('?',) * len(assoc_ids))})"
        )
        args.extend(assoc_ids)

    if tags:
        quals.append(
            f"""ip_address IN (
                    SELECT resource_name FROM tags
                    WHERE
                      resource_type = 'ip_address'
                      AND (tagname, tagvalue) IN (
                          {','.join(('(?, ?)',) * len(tags))}
                      )
            """
        )
        args.extend(itertools.chain.from_iterable(tags))

    query = """
        SELECT
            ip_address,
            instance_id,
            allocation_id,
            association_id
        FROM
            ip_addresses
    """
    if quals:
        query += f" WHERE {' AND '.join(quals)}"

    cur = app["db"].cursor()
    cur.execute(f"""
        SELECT resource_name FROM tags
        WHERE tagname = ? AND resource_type = 'volume'
        AND tagvalue IN ({",".join(["?"] * len(tagvalue))})
    """, [tagname] + list(tagvalue))
    addresses = cur.fetchall()
    cur.close()

    return {
        "addressesSet": [{
            "publicIp": addr[0],
            "instanceId": addr[1],
            "allocationId": addr[2],
            "associationId": addr[3],
            "domain": "vpc",
        } for addr in addresses],
    }


@_routing.handler("AllocateAddress")
async def allocate_address(
    args: _routing.HandlerArgs,
    app: _routing.App,
) -> Dict[str, Any]:
    address = args.get("Address")
    if address:
        raise _routing.InvalidParameterError(
            "claiming existing addresses is not supported")

    domain = args.get("Domain")
    if domain and domain != "vpc":
        raise _routing.InvalidParameterError(
            "standard domain is not supported")

    cur = app["db"].cursor()
    cur.execute(f"""
        SELECT ip_address FROM ip_addresses
    """)
    existing = {ipaddress.IPv4Address(row[0]) for row in cur.fetchall()}
    cur.close()

    net = objects.network_from_xml(app['libvirt_net'].XMLDesc())
    ip_range_start, ip_range_end = net.static_ip_range
    for int_addr in range(int(ip_range_start), int(ip_range_end)):
        address = ipaddress.IPv4Address(int_addr)
        if address not in existing:
            break
    else:
        raise AddressLimitExceededError(
            "libvirt network is out of static addresses")

    tags = {}
    tag_spec = args.get("TagSpecification")
    if tag_spec:
        for spec_entry in tag_spec:
            tag_entries = spec_entry["Tag"]
            for tag in tag_entries:
                tags[tag["Key"]] = tag["Value"]

    cur = app["db"].cursor()

    if tags:
        cur.executemany(
            """
                INSERT INTO tags
                    (resource_name, resource_type, tagname, tagvalue)
                VALUES (?, ?, ?, ?)
            """,
            [[address, "ip_address", n, v] for n, v in tags.items()],
        )

    allocation_id = f"eipalloc-{uuid.uuid4()}"
    cur.execute(
        """
            INSERT INTO ip_addresses
                (allocation_id, ip_address)
            VALUES (?, ?)
        """,
        [allocation_id, str(address)],
    )

    app["db"].commit()

    return {
        "publicIp": str(address),
        "domain": "vpc",
        "allocationId": allocation_id,
    }


@_routing.handler("AssociateAddress")
async def associate_address(
    args: _routing.HandlerArgs,
    app: _routing.App,
) -> Dict[str, Any]:
    pool: libvirt.virStoragePool = app['libvirt_pool']

    alloc_id = args.get("AllocationId")
    if not alloc_id:
        raise _routing.InvalidParameterError(
            "missing required AllocationId")

    instance_id = args.get("InstanceId")
    if not alloc_id:
        raise _routing.InvalidParameterError(
            "missing required InstanceId")

    vir_conn = pool.connect()
    try:
        new_virdom = vir_conn.lookupByName(instance_id)
    except libvirt.libvirtError as e:
        raise errors.InvalidInstanceID_NotFound(
            f"invalid InstanceId: {e}") from e

    assoc_id = f"eipassoc-{uuid.uuid4()}"

    db_conn = app["db"]

    with db_conn:
        cur = db_conn.execute(
            """
                SELECT instance_id, ip_address
                FROM ip_addresses
                WHERE allocation_id = ?
            """,
            [alloc_id],
        )

        row = cur.fetchone()
        if row is None:
            raise InvalidAddressID_NotFound(
                "could not find address for specified AllocationId")

        cur_instance_id, ip_address = row

    if cur_instance_id is not None:
        try:
            cur_virdom = vir_conn.lookupByName(cur_instance_id)
        except libvirt.libvirtError:
            app["logger"].warning(
                "cannot find currently associated instance",
                exc_info=True,
            )
        else:
            await qemu.agent_exec(
                cur_virdom,
                ["ip", "addr", "del", ip_address, "dev", "vif0"],
            )

    result = await qemu.agent_exec(
        new_virdom,
        ["ip", "addr", "add", ip_address, "dev", "vif0"],
    )

    if result.returncode != 0:
        raise _routing.InternalServerError(
            f"could not assign address in VM: {result.returncode}, "
            f"{result.stderr.read()}"
        )

    with db_conn:
        db_conn.execute(
            """
                UPDATE
                    ip_addresses
                SET
                    association_id = ?,
                    instance_id = ?
                WHERE
                    allocation_id = ?
            """,
            [assoc_id, instance_id, alloc_id],
        )

    return {
        "return": "true",
        "associationId": assoc_id,
    }


@_routing.handler("DisassociateAddress")
async def disassociate_address(
    args: _routing.HandlerArgs,
    app: _routing.App,
) -> Dict[str, Any]:
    pool: libvirt.virStoragePool = app['libvirt_pool']

    assoc_id = args.get("AssociationId")
    if not assoc_id:
        raise _routing.InvalidParameterError(
            "missing required AssociationId")

    db_conn = app["db"]

    with db_conn:
        cur = db_conn.execute(
            """
                SELECT instance_id, ip_address
                FROM ip_addresses
                WHERE association_id = ?
            """,
            [assoc_id],
        )

        row = cur.fetchone()
        if row is None:
            raise InvalidAssociationID_NotFound(
                "could not find address for specified AssociationId")

        cur_instance_id, ip_address = row

    if cur_instance_id is not None:
        vir_conn = pool.connect()

        try:
            cur_virdom = vir_conn.lookupByName(cur_instance_id)
        except libvirt.libvirtError:
            app["logger"].warning(
                "cannot find currently associated instance",
                exc_info=True,
            )
        else:
            await qemu.agent_exec(
                cur_virdom,
                ["ip", "addr", "del", ip_address, "dev", "vif0"],
            )

    with db_conn:
        db_conn.execute(
            """
                UPDATE
                    ip_addresses
                SET
                    association_id = NULL,
                    instance_id = NULL
                WHERE
                    association_id = ?
            """,
            [assoc_id],
        )

    return {
        "return": "true",
    }