import dbus
import json
import socket
import struct
import time
import asyncio
import os
from typing import Optional
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import PingRequest, EmptyResult

mcp = FastMCP("NetworkManager MCP Server", host="0.0.0.0", port=8000, stateless_http=True, json_response=True)

DEVICE_TYPES = {
    0: "Unknown", 1: "Ethernet", 2: "Wi-Fi", 5: "Bluetooth", 6: "OLPC",
    7: "WiMAX", 8: "Modem", 9: "InfiniBand", 10: "Bond", 11: "VLAN",
    12: "ADSL", 13: "Bridge", 14: "Generic", 15: "Team", 16: "TUN",
    17: "IPTunnel", 18: "MACVLAN", 19: "VXLAN", 20: "Veth",
}

DEVICE_STATES = {
    0: "Unknown", 10: "Unmanaged", 20: "Unavailable", 30: "Disconnected",
    40: "Prepare", 50: "Config", 60: "Need Auth", 70: "IP Config",
    80: "IP Check", 90: "Secondaries", 100: "Activated", 110: "Deactivating", 120: "Failed",
}

CONNECTIVITY_STATES = {
    0: "Unknown", 1: "None", 2: "Portal", 3: "Limited", 4: "Full",
}

def dbus_to_python(data):
    """Recursively convert D-Bus types to standard Python types."""
    if isinstance(data, (dbus.String, dbus.ObjectPath)):
        return str(data)
    elif isinstance(data, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte)):
        return int(data)
    elif isinstance(data, dbus.Boolean):
        return bool(data)
    elif isinstance(data, dbus.Double):
        return float(data)
    elif isinstance(data, dbus.Array):
        return [dbus_to_python(item) for item in data]
    elif isinstance(data, dbus.Dictionary):
        return {dbus_to_python(key): dbus_to_python(value) for key, value in data.items()}
    elif isinstance(data, (list, tuple)):
        return [dbus_to_python(item) for item in data]
    elif isinstance(data, dict):
        return {dbus_to_python(key): dbus_to_python(value) for key, value in data.items()}
    return data

def get_ip_config_details(bus, path, interface_name):
    if not path or path == "/": return {"addresses": [], "routes": []}
    try:
        proxy = bus.get_object("org.freedesktop.NetworkManager", path)
        prop_iface = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
        props = prop_iface.GetAll(interface_name)
        p = dbus_to_python(props)
        return {"addresses": p.get("AddressData", []), "routes": p.get("RouteData", [])}
    except Exception: return {"addresses": [], "routes": []}

class NMTransaction:
    """Helper to handle Checkpoint -> Modify -> Check -> Rollback/Commit flow."""
    def __init__(self, bus, ctx: Context, timeout=60):
        self.bus = bus
        self.ctx = ctx
        self.timeout = timeout
        self.checkpoint = None
        self.nm_proxy = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        self.manager = dbus.Interface(self.nm_proxy, "org.freedesktop.NetworkManager")
        self.props = dbus.Interface(self.nm_proxy, "org.freedesktop.DBus.Properties")

    def get_connectivity(self):
        return int(self.props.Get("org.freedesktop.NetworkManager", "Connectivity"))

    async def run(self, action_fn):
        initial_conn = self.get_connectivity()
        await self.ctx.info(f"Initial connectivity: {CONNECTIVITY_STATES.get(initial_conn)}")

        await self.ctx.info("Creating NetworkManager checkpoint...")
        devices = self.manager.GetDevices()
        self.checkpoint = self.manager.CheckpointCreate(devices, self.timeout, 1) # 1 = Destroy all other checkpoints

        try:
            await self.ctx.info("Applying changes...")
            await action_fn()

            # Wait a bit for changes to propagate/re-activation
            await asyncio.sleep(2)

            # Check that session is still alive otherwise issue a rollback
            try:
                await asyncio.wait_for(self.ctx.session.send_request(PingRequest(), result_type=EmptyResult), timeout=5)
            except (asyncio.TimeoutError, Exception) as e:
                if self.checkpoint:
                    try:
                        self.manager.CheckpointRollback(self.checkpoint)
                    except Exception as rb_err:
                        print(f"Failed to rollback: {str(rb_err)}")
                return {"status": "error", "message": "MCP Session unresponsive after change. Changes rolled back."}

            new_conn = self.get_connectivity()
            await self.ctx.info(f"New connectivity: {CONNECTIVITY_STATES.get(new_conn)}")

            self.manager.CheckpointDestroy(self.checkpoint)
            return {"status": "success", "message": "Changes applied and committed."}

        except Exception as e:
            await self.ctx.error(f"Error: {str(e)}")
            if self.checkpoint:
                await self.ctx.info("Rolling back...")
                try: self.manager.CheckpointRollback(self.checkpoint)
                except Exception: pass
            raise e

@mcp.tool()
async def get_connectivity() -> str:
    """Gets the global network connectivity state (e.g., Full, Limited, None)."""
    try:
        bus = dbus.SystemBus()
        nm_proxy = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        nm_prop_iface = dbus.Interface(nm_proxy, "org.freedesktop.DBus.Properties")
        val = nm_prop_iface.Get("org.freedesktop.NetworkManager", "Connectivity")
        return CONNECTIVITY_STATES.get(val, "Unknown")
    except Exception as e: return f"Error: {str(e)}"

@mcp.tool()
async def get_dns() -> str:
    """Gets the system DNS configuration (nameservers, priority, interface)."""
    try:
        bus = dbus.SystemBus()
        dns_proxy = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/DnsManager")
        raw = dbus_to_python(dbus.Interface(dns_proxy, "org.freedesktop.DBus.Properties").Get("org.freedesktop.NetworkManager.DnsManager", "Configuration"))
        dns_config = [{"servers": e.get("nameservers", []), "priority": e.get("priority"), "interface": e.get("interface")} for e in raw]
        return json.dumps(dns_config, indent=2)
    except Exception as e: return f"Error: {str(e)}"

@mcp.tool()
async def get_devices() -> str:
    """Gets a list of all network devices and their current status (interface, type, state, mac)."""
    try:
        bus = dbus.SystemBus()
        nm_proxy = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        nm_manager = dbus.Interface(nm_proxy, "org.freedesktop.NetworkManager")
        devices = []
        for d_path in nm_manager.GetDevices():
            try:
                dev_proxy = bus.get_object("org.freedesktop.NetworkManager", d_path)
                p = dbus_to_python(dbus.Interface(dev_proxy, "org.freedesktop.DBus.Properties").GetAll("org.freedesktop.NetworkManager.Device"))
                if "DeviceType" in p: p["DeviceTypeString"] = DEVICE_TYPES.get(p["DeviceType"], "Unknown")
                if "State" in p: p["StateString"] = DEVICE_STATES.get(p["State"], "Unknown")
                devices.append({"interface": p.get("Interface"), "type": p.get("DeviceTypeString"), "state": p.get("StateString"), "mac_address": p.get("HwAddress")})
            except Exception: continue
        return json.dumps(devices, indent=2)
    except Exception as e: return f"Error: {str(e)}"

@mcp.tool()
async def get_connections() -> str:
    """Gets all configured connection profiles, including IPv4/IPv6 settings and active status."""
    try:
        bus = dbus.SystemBus()
        nm_proxy = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        nm_prop_iface = dbus.Interface(nm_proxy, "org.freedesktop.DBus.Properties")
        active_info = {}
        try:
            for ac_path in nm_prop_iface.Get("org.freedesktop.NetworkManager", "ActiveConnections"):
                try:
                    ac_proxy = bus.get_object("org.freedesktop.NetworkManager", ac_path)
                    ac_p = dbus_to_python(dbus.Interface(ac_proxy, "org.freedesktop.DBus.Properties").GetAll("org.freedesktop.NetworkManager.Connection.Active"))
                    active_info[str(ac_p.get("Uuid"))] = {"ip4_config": str(ac_p.get("Ip4Config")), "ip6_config": str(ac_p.get("Ip6Config"))}
                except Exception: continue
        except Exception: pass
        settings_proxy = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings")
        settings_iface = dbus.Interface(settings_proxy, "org.freedesktop.NetworkManager.Settings")
        connections = []
        for c_path in settings_iface.ListConnections():
            try:
                con_proxy = bus.get_object("org.freedesktop.NetworkManager", c_path)
                config = dbus_to_python(dbus.Interface(con_proxy, "org.freedesktop.NetworkManager.Settings.Connection").GetSettings())
                s_con = config.get("connection", {}); uuid = s_con.get("uuid")
                ipv4_s = config.get("ipv4", {}); ipv6_s = config.get("ipv6", {})
                is_active = uuid in active_info
                ipv4_data = {"method": ipv4_s.get("method"), "addresses": ipv4_s.get("address-data", []), "routes": ipv4_s.get("route-data", [])}
                ipv6_data = {"method": ipv6_s.get("method"), "addresses": ipv6_s.get("address-data", []), "routes": ipv6_s.get("route-data", [])}
                if is_active:
                    info = active_info[uuid]
                    a4 = get_ip_config_details(bus, info["ip4_config"], "org.freedesktop.NetworkManager.IP4Config")
                    if a4["addresses"]: ipv4_data["addresses"] = a4["addresses"]
                    if a4["routes"]: ipv4_data["routes"] = a4["routes"]
                    a6 = get_ip_config_details(bus, info["ip6_config"], "org.freedesktop.NetworkManager.IP6Config")
                    if a6["addresses"]: ipv6_data["addresses"] = a6["addresses"]
                    if a6["routes"]: ipv6_data["routes"] = a6["routes"]
                connections.append({"name": s_con.get("id"), "uuid": uuid, "type": s_con.get("type"), "interface-name": s_con.get("interface-name"), "active": is_active, "ipv4": ipv4_data, "ipv6": ipv6_data})
            except Exception: continue
        return json.dumps(connections, indent=2)
    except Exception as e: return f"Error: {str(e)}"

@mcp.tool()
async def set_connection_state(connection_uuid: str, active: bool, ctx: Context) -> str:
    """
    Activates (up) or deactivates (down) a connection profile by UUID.
    Includes safety checkpoint and connectivity check with interactive confirmation.
    """
    bus = dbus.SystemBus()
    tx = NMTransaction(bus, ctx)
    async def action():
        nm_proxy = bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
        manager = dbus.Interface(nm_proxy, "org.freedesktop.NetworkManager")
        settings_path = dbus.Interface(bus.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings"), "org.freedesktop.NetworkManager.Settings").GetConnectionByUuid(connection_uuid)
        if active: await ctx.info(f"Activating {connection_uuid}..."); manager.ActivateConnection(settings_path, "/", "/")
        else:
            await ctx.info(f"Deactivating {connection_uuid}...")
            active_paths = dbus.Interface(nm_proxy, "org.freedesktop.DBus.Properties").Get("org.freedesktop.NetworkManager", "ActiveConnections")
            for ac_path in active_paths:
                if str(dbus.Interface(bus.get_object("org.freedesktop.NetworkManager", ac_path), "org.freedesktop.DBus.Properties").Get("org.freedesktop.NetworkManager.Connection.Active", "Uuid")) == connection_uuid:
                    manager.DeactivateConnection(ac_path); break
    return json.dumps(await tx.run(action), indent=2)

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "http" or transport == "streamable-http":
        mcp.run(transport="streamable-http")
    else: mcp.run(transport="stdio")
