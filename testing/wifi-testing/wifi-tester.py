#!/usr/bin/python3

import xml.etree.ElementTree as ET
import json
import csv
import operator
import base64
import os.path
import os
import sys
import shutil
import urllib.request, urllib.error, urllib.parse
import json
import time
import uuid
import re
import argparse
import fnmatch
from datetime import date

import socket
import asyncio
import threading
# import subprocess
# import shlex

# On NUC
# N.B. if hostapd running on host for this app then all requests go to local server
# systemctl stop hostapd
# brctl delif br0 wlp0s20f3 to remove nucs internal wifi
# after which connect_wifi will connect it

# both the wpa_supplicant and dhclient commands throw errors, but still succeed
# rfkill: Cannot get wiphy information
# ioctl[SIOCSIWAP]: Operation not permitted
# ioctl[SIOCSIWENCODEEXT]: Invalid argument
# ioctl[SIOCSIWENCODEEXT]: Invalid argument
# contrary to googling -D wext is necessary
# these errors do not seem to prevent a connection to iiab ssid

# N.B on rpi iw does not work with tplink 725
# looks like same on NUC - only Canakit and edimax
# ralink chip works
# tried  modprobe mac80211_hwsim radios=8 - ip a shows wlan0 to wlan15 plus hwsim0
# ip link set wlan15 up no complaints
# wpa_supplicant says successful
# dhclient -4 wlan15 hangs
# but I now have iw list |grep Wiphy phy0 to phy8
# tried wlan10. also hangs

# cat  /sys/class/net/wlx801f02bbfa46/phy80211/name can relate device to phy#
# one gets rename28 (wlx801f02bbfa46) and the other gets similar name (wlx000f6003c82f gets wlx000f6003c828)
# one (wlx801f02bbfa46)seems to allow multiple clones and now doesn't rename, but all have the same mac address
# assign mac address works on edimmax but canakit does automatically and ignores parameter
# cat /var/lib/dhcp/dhclient.leases for leases

usb_wifi_ifaces = {}
total_connections = 0
monitor_iface = None

CLONE_DEVS = False
VDEV_SUFFIX = 'vdev'
VMAC_BASE = '02:00:00:00:00:' # for local 2nd least significant bit of first octet must be 1 (2, 6, A, E)
START_URL = '/testing/index.html' # no protocol or host
SLEEP_SECONDS = 1
WIFI_DEV_PREFIX = 'wl'
ESC='\033'

vmac_counter = 0
run_flag = True
verbose = False

async def main_async():
    global total_connections
    global monitor_iface

    print('Starting')
    threading.Thread(target=key_capture_thread, args=(), name='key_capture_thread', daemon=True).start()
    print('Press the ENTER key to terminate')

    await init()

    for dev in usb_wifi_ifaces:
        if not monitor_iface: # pick first dev as monitor
            monitor_iface = dev
            await connect_wifi(dev)
            usb_wifi_ifaces[dev]['task'] = asyncio.create_task(monitor_dev(dev))
        else:
            usb_wifi_ifaces[dev]['task'] = asyncio.create_task(one_dev(dev))

    while run_flag:

        await asyncio.sleep(SLEEP_SECONDS) # vamp until ready

    # now wait for all async to terminate
    for dev in usb_wifi_ifaces:
        if 'task' in usb_wifi_ifaces[dev]:
            await usb_wifi_ifaces[dev]['task']
    await reset_all_ifaces()

async def monitor_dev(dev):
    global total_connections
    global usb_wifi_ifaces

    # await connect_wifi(dev) # try to get a connection
    url = '/stat'
    while run_flag:
        client_ip = usb_wifi_ifaces[dev].get('ip-addr', None)
        #print(client_ip)
        if client_ip:
            header, stat_json = await get_html(client_ip, url, port=10080)
            #print(header)
            if header['status'] >= '500':
                print('Error Connecting to Status Server')
            else:
                show_stat(stat_json)
        else:
            await connect_wifi(dev)
        await asyncio.sleep(5)

def show_stat(stat_json):
    global total_connections
    global usb_wifi_ifaces

    clear_line = ESC+'[0K'

    if not verbose:
        print(ESC+'[0;0H') # cursor to top of screen
    server_mac_arr = json.loads(stat_json)
    client_mac_arr = {}
    print(f'Total Connections on Server: {len(server_mac_arr)}{clear_line}')
    print(clear_line)
    print(f'These are our interfaces:{clear_line}')
    for dev in usb_wifi_ifaces:
        if 'ip-addr' in usb_wifi_ifaces[dev]:
            ip_addr = usb_wifi_ifaces[dev]['ip-addr']
            print(f'{dev} has IP Addr {ip_addr}{clear_line}')
        if 'mac' in usb_wifi_ifaces[dev]:
            client_mac_arr[usb_wifi_ifaces[dev]['mac']] = dev
            if usb_wifi_ifaces[dev]['mac'] in server_mac_arr:
                print(f'{dev}: connected to server{clear_line}')
        else:
            print(f'Why no mac for {dev}{clear_line}')
    print(clear_line)
    print(f'These mac addresses seen on the server:{clear_line}')
    for mac in server_mac_arr:
        if mac in client_mac_arr:
            print (f'{mac} ( {client_mac_arr[mac]} ) connected to server{clear_line}')
        else:
            print(f'{mac}  on server is not ours{clear_line}')
    #print('Server has additional ' + str(len(mac_arr - total_connections)) + ' connections')

async def one_dev(dev):
    global total_connections
    global usb_wifi_ifaces

    await connect_wifi(dev) # try to get a connection
    url = START_URL
    while run_flag:
        client_ip = usb_wifi_ifaces[dev].get('ip-addr', None)
        if client_ip:
            header, html = await get_html(client_ip, url)
        else:
            await connect_wifi(dev)
        await asyncio.sleep(SLEEP_SECONDS)

async def get_html(client_ip, page_url, port=80, server_ip='172.18.96.1'):
    print_msg('Retrieving page for ' + client_ip)
    BUF_SIZE = 4096

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((client_ip, 0)) # use ip of interface
        try:
            s.connect((server_ip , port))
        except ConnectionRefusedError as e:
            print ('socket error: ' + str(e))
            header = {'status': '503', 'content-length': '0'} # pseudo status for not connection
            html = ''
            return header, html
        head = 'HEAD ' +  page_url +  ' HTTP/1.1\r\nHost: ' + server_ip + '\r\nAccept: text/html\r\n\r\n'
        request = 'GET ' +  page_url +  ' HTTP/1.1\r\nHost: ' + server_ip + '\r\nAccept: text/html\r\n\r\n'
        s.sendall(request.encode('utf8'))
        header = b''
        while True:
            buf = s.recv(BUF_SIZE)
            if not header:
                parts = buf.split(b'\r\n\r\n',1)
                header = parse_header(parts[0].decode())
                html_bytes = parts[1]
            else:
                html_bytes += buf
            if len(html_bytes) >= int(header['content-length']):
            # if len(buf) < BUF_SIZE: # this can fail if header and body are two fetches
                break
        html = html_bytes.decode()
        print_msg (header)
    return header, html

async def init():
    global total_connections
    vdevs = {}
    total_connections = 0

    if not verbose:
        print(ESC+'[0;0H') # cursor to top of screen
        print(ESC+'[2J')   # clear screen
    print('Starting init')

    # remove existing connections
    await reset_all_ifaces()

    await find_wifi_dev() # get dict of physical wifi devices
    #for dev in usb_wifi_ifaces: # reset any wpa services
    #    await reset_wifi_conn(dev)

    if CLONE_DEVS:
        await find_wiphy() # find support for iw but can return false positive
        for dev in usb_wifi_ifaces:
            if VDEV_SUFFIX in dev: # it's virtual
                continue
            wiphy = usb_wifi_ifaces[dev].get('wiphy', None)
            if wiphy:
                vdev = dev + VDEV_SUFFIX
                if await add_vdev(wiphy, vdev):
                    vdevs[vdev] = {'vdev': True}
        await find_wifi_dev()

async def add_vdev(phy, vdev):
    global vmac_counter
    cmdstr = 'iw phy ' + phy +' interface add ' + vdev + ' type managed'
    compl_proc = await subproc_run(cmdstr)
    if compl_proc.returncode == 0:
        # now override mac address
        vmac_counter += 1
        cmdstr = 'ip link set ' + vdev + ' address ' + VMAC_BASE + str(vmac_counter).zfill(2)
        compl_proc = await subproc_run(cmdstr) # this can fail when new mac automatically assigned (i.e. Canakit)
        return True
    else:
        return False

async def connect_wifi(iface):
    # wpa_cli -i wlxc4e98408a7be disconnect
    # wpa_cli -i wlan6 terminate - fails if not running
    if usb_wifi_ifaces[iface]['status'] != 'UP':
        print_msg('Starting set link up for ' + iface)
        cmdstr = 'ip link set ' + iface + ' up'
        compl_proc = await subproc_run(cmdstr)
        if compl_proc.returncode != 0:
            print('Set link up failed for ' + iface)
            print(compl_proc.stderr)

    print_msg('Resetting wpa_supplicant for ' + iface)
    await reset_wifi_conn(iface) # close and wpa services

    print_msg('Starting wpa_supplicant -B -i ' + iface + ' -c wpa_iiab.conf -D wext')
    cmdstr = 'wpa_supplicant -B -i ' + iface + ' -c wpa_iiab.conf -D wext'
    compl_proc = await subproc_run(cmdstr)
    if compl_proc.returncode != 0:
        print('wpa_supplicant connection failed for ' + iface)
        return False

    print_msg('Starting dhclient ' + iface)
    cmdstr = 'dhclient -4 ' + iface
    compl_proc = await subproc_run(cmdstr)
    if compl_proc.returncode != 0:
        print('dhclient failed to get IP Address for ' + iface)
        return False
    else:
        print_msg('Connection successful for ' + iface)
        # now get the ip addr
        await get_wifi_dev_ip(iface)
        return True

async def reset_all_ifaces():
    await subproc_run('killall dhclient')
    await subproc_run('killall wpa')
    try:
        os.remove('/var/lib/dhcp/dhclient.leases') # get rid of existing leases
    except OSError:
        pass

async def reset_wifi_conn(iface, force=False):
    cmdstr = 'wpa_cli -i ' + iface + ' status'
    compl_proc = await subproc_run(cmdstr)
    if compl_proc.returncode != 0 and force==False:
        return # assume nothing to reset
    cmdstr = 'wpa_cli -i ' + iface + ' disconnect'
    compl_proc = await subproc_run(cmdstr)
    cmdstr = 'wpa_cli -i ' + iface + ' terminate'
    compl_proc = await subproc_run(cmdstr)

async def find_wifi_dev(filter=WIFI_DEV_PREFIX, virt=VDEV_SUFFIX):
    global usb_wifi_ifaces
    usb_wifi_ifaces = {}
    compl_proc = await subproc_run('ip a')
    lines = compl_proc.stdoutarr
    for l in lines:
        if ':' in l and filter in l:
            wifi_info_split = l.split()
            wifi_dev = wifi_info_split[1][:-1]
            if virt in wifi_dev:
                vdev = True
            else:
                vdev = False
            if wifi_dev not in usb_wifi_ifaces:
                dev_info = {}
                dev_info['status'] = wifi_info_split[8]
                usb_wifi_ifaces[wifi_dev] = dev_info
            else:
                usb_wifi_ifaces[wifi_dev]['status'] = wifi_info_split[8]
            usb_wifi_ifaces[wifi_dev]['vdev'] = vdev

async def find_wiphy():
    global usb_wifi_ifaces
    for dev in usb_wifi_ifaces:
        cmdstr = 'cat /sys/class/net/' + dev + '/phy80211/name'
        compl_proc = await subproc_run(cmdstr)
        if compl_proc.returncode == 0:
            wiphy = compl_proc.stdout.split('\n')[0]
            usb_wifi_ifaces[dev]['wiphy'] = wiphy

async def get_wifi_dev_ip(iface):
    global usb_wifi_ifaces
    wifi_info = {}
    cmdstr = 'ip  a show dev ' + iface
    compl_proc = await subproc_run(cmdstr)
    lines = compl_proc.stdout.split('\n')
    for l in lines:
        if 'inet ' in l: # only ipv4
            wifi_info_split = l.split()
            if len(wifi_info_split) != 0:
                wifi_info['ip-addr'] = wifi_info_split[1].split('/')[0]
        if 'link/ether ' in l: # mac
            wifi_info_split = l.split()
            if len(wifi_info_split) != 0:
                wifi_info['mac'] = wifi_info_split[1]
    usb_wifi_ifaces[iface].update(wifi_info)

def parse_header(header_str, delim='\r\n'):
    hdr_dict = {}
    lines = header_str.split(delim)
    hdr_dict['status'] = lines[0].split()[1]
    for l in lines[1:]:
        if ': ' in l:
            key, val = l.split(': ')
            hdr_dict[key.lower()] = val
    return hdr_dict

async def subproc_run(cmd):
    class ComplProc(object):
        returncode = None
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    ComplProc.stdout = stdout.decode()
    ComplProc.stderr = stderr.decode()
    ComplProc.returncode = proc.returncode
    ComplProc.stdoutarr = stdout.decode().split('\n')
    return ComplProc

def print_msg(msg):
    if verbose:
        print(msg)

def key_capture_thread():
    global run_flag
    input()
    run_flag = False

if __name__ == "__main__":
    # Now run the main routine
    asyncio.run(main_async())