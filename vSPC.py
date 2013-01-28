#!/usr/bin/python -u

# Copyright 2011 Isilon Systems LLC. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification, are
# permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice, this list of
#       conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright notice, this list
#       of conditions and the following disclaimer in the documentation and/or other materials
#       provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY ISILON SYSTEMS LLC. ''AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
# USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
# OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are those of the
# authors and should not be interpreted as representing official policies, either expressed
# or implied, of <copyright holder>.

"""
vSPC.py - A Virtual Serial Port Concentrator for VMware

Run 'vSPC.py -h' for full help.

This server is based on publicly available documentation:
  http://www.vmware.com/support/developer/vc-sdk/visdk41pubs/vsp41_usingproxy_virtual_serial_ports.pdf
"""

from __future__ import with_statement

__author__ = "Zachary M. Loafman"
__copyright__ = "Copyright (C) 2011 Isilon Systems LLC."
__revision__ = "$Id$"

import getopt
import fcntl
import logging
import os
import pickle
import select
import socket
import ssl
import struct
import sys
import termios
import threading
import time
import traceback
import Queue
from telnetlib import *
from telnetlib import IAC,DO,DONT,WILL,WONT,BINARY,ECHO,SGA,SB,SE,NOOPT,theNULL

from lib.poll import Poller, Selector
from lib.telnet import TelnetServer, VMTelnetServer, VMExtHandler, hexdump
from lib.backend import vSPCBackendMemory, vSPCBackendFile
from lib.admin import Q_VERS, Q_NAME, Q_UUID, Q_PORT, Q_OK, Q_VM_NOTFOUND, Q_LOCK_EXCL, Q_LOCK_WRITE, Q_LOCK_FFA, Q_LOCK_FFAR, Q_LOCK_BAD, Q_LOCK_FAILED

LISTEN_BACKLOG = 5
CLIENT_ESCAPE_CHAR = chr(29)

def openport(port, use_ssl=False, ssl_cert=None, ssl_key=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if use_ssl:
        sock = ssl.wrap_socket(sock, keyfile=ssl_key, certfile=ssl_cert)
    sock.setblocking(0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1);
    sock.bind(("", port))
    sock.listen(LISTEN_BACKLOG)
    return sock

class vSPC(Poller, VMExtHandler):
    class Vm:
        def __init__(self, uuid = None, name = None, vts = None):
            self.vts = vts if vts else []
            self.clients = []
            self.uuid = uuid
            self.name = name
            self.port = None
            self.listener = None
            self.last_time = None
            self.vmotion = None

        def fileno(self):
            return self.listener.fileno()

    class Client(TelnetServer):
        def __init__(self, sock,
                     server_opts = (BINARY, SGA, ECHO),
                     client_opts = (BINARY, SGA)):
            TelnetServer.__init__(self, sock, server_opts, client_opts)
            self.uuid = None

    def __init__(self, proxy_port, admin_port,
                 vm_port_start, vm_expire_time, backend, use_ssl=False,
                 ssl_cert=None, ssl_key=None):
        Poller.__init__(self)

        self.proxy_port = proxy_port
        self.admin_port = admin_port
        if not vm_port_start: # account for falsey things, not just None
            vm_port_start = None
        self.vm_port_next = vm_port_start
        self.vm_expire_time = vm_expire_time
        self.backend = backend

        self.orphans = []
        self.vms = {}
        self.ports = {}
        self.vmotions = {}
        self.do_ssl = use_ssl
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key

        self.task_queue = Queue.Queue()
        self.task_queue_threads = []

    def _queue_run(self, queue):
        while True:
            try:
                queue.get()()
            except Exception, e:
                logging.exception("Worker exception caught")

    def task_queue_run(self):
        self._queue_run(self.task_queue)

    def start(self):
        self.task_queue_threads.append(self._start_thread(self.task_queue_run))

    def _start_thread(self, f):
        th = threading.Thread(target = f)
        th.daemon = True
        th.start()

        return th

    def send_buffered(self, ts, s = ''):
        if ts.send_buffered(s):
            self.add_writer(ts, self.send_buffered)
        else:
            self.del_writer(ts)

    def new_vm_connection(self, sock):
        sock.setblocking(0)
        sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        vt = VMTelnetServer(sock, handler = self)
        self.add_reader(vt, self.queue_new_vm_data)

    def queue_new_vm_connection(self, listener):
        try:
            sock = listener.accept()[0]
        except ssl.SSLError:
            return

        self.task_queue.put(lambda: self.new_vm_connection(sock))

    def new_client_connection(self, sock, vm):
        sock.setblocking(0)
        sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)

        client = self.Client(sock)
        client.uuid = vm.uuid

        self.add_reader(client, self.queue_new_client_data)
        vm.clients.append(client)

        logging.debug('uuid %s new client, %d active clients'
                      % (client.uuid, len(vm.clients)))

    def queue_new_client_connection(self, vm):
        sock = vm.listener.accept()[0]
        self.task_queue.put(lambda: self.new_client_connection(sock, vm))

    def abort_vm_connection(self, vt):
        if vt.uuid and vt in self.vms[vt.uuid].vts:
            logging.debug('uuid %s VM socket closed' % vt.uuid)
            self.vms[vt.uuid].vts.remove(vt)
            self.stamp_orphan(self.vms[vt.uuid])
        else:
            logging.debug('unidentified VM socket closed')
        self.del_all(vt)
        with self.lock:
            self.remove_fd(vt)
        vt.close()

    def new_vm_data(self, vt):
        neg_done = False
        try:
            neg_done = vt.negotiation_done()
        except (EOFError, IOError, socket.error):
            self.abort_vm_connection(vt)
            return

        if not neg_done:
            self.add_reader(vt, self.queue_new_vm_data)
            return

        # Queue VM data during vmotion
        if vt.uuid and self.vms[vt.uuid].vmotion:
            self.add_reader(vt, self.queue_new_vm_data)
            return

        s = None
        try:
            s = vt.read_very_lazy()
        except (EOFError, IOError, socket.error):
            self.abort_vm_connection(vt)
            return

        if not s: # May only be option data, or exception
            self.add_reader(vt, self.queue_new_vm_data)
            return

        if not vt.uuid or not self.vms.has_key(vt.uuid):
            # In limbo, no one can hear you scream
            self.add_reader(vt, self.queue_new_vm_data)
            return

        # logging.debug('new_vm_data %s: %s' % (vt.uuid, repr(s)))
        self.backend.notify_vm_msg(vt.uuid, vt.name, s)

        clients = self.vms[vt.uuid].clients[:]
        for cl in clients:
            try:
                self.send_buffered(cl, s)
            except (EOFError, IOError, socket.error), e:
                logging.debug('cl.socket send error: %s' % (str(e)))
                self.abort_client_connection(cl)
        self.add_reader(vt, self.queue_new_vm_data)

    def queue_new_vm_data(self, vt):
        # Don't alert repeatedly on the same input
        self.del_reader(vt)
        self.task_queue.put(lambda: self.new_vm_data(vt))

    def abort_client_connection(self, client):
        logging.debug('uuid %s client socket closed, %d active clients' %
                      (client.uuid, len(self.vms[client.uuid].clients)-1))
        if client in self.vms[client.uuid].clients:
            self.vms[client.uuid].clients.remove(client)
            self.stamp_orphan(self.vms[client.uuid])
        self.del_all(client)
        self.backend.notify_client_del(client.sock, client.uuid)

    def new_client_data(self, client):
        neg_done = False
        try:
            neg_done = client.negotiation_done()
        except (EOFError, IOError, socket.error):
            self.abort_client_connection(client)
            return

        if not neg_done:
            self.add_reader(client, self.queue_new_client_data)
            return

        # Queue VM data during vmotion
        if self.vms[client.uuid].vmotion:
            self.add_reader(client, self.queue_new_client_data)
            return

        s = None
        try:
            s = client.read_very_lazy()
        except (EOFError, IOError, socket.error):
            self.abort_client_connection(client)
            return

        if not s: # May only be option data, or exception
            self.add_reader(client, self.queue_new_client_data)
            return

        # logging.debug('new_client_data %s: %s' % (client.uuid, repr(s)))

        for vt in self.vms[client.uuid].vts:
            try:
                self.send_buffered(vt, s)
            except (EOFError, IOError, socket.error), e:
                logging.debug('cl.socket send error: %s' % (str(e)))
        self.add_reader(client, self.queue_new_client_data)

    def queue_new_client_data(self, client):
        # Don't alert repeatedly on the same input
        self.del_reader(client)
        self.task_queue.put(lambda: self.new_client_data(client))

    def new_vm(self, uuid, name, port = None, vts = None):
        vm = self.Vm(uuid = uuid, name = name, vts = vts)

        self.open_vm_port(vm, port)
        self.vms[uuid] = vm

        # Only notify if we generated the port
        if not port:
            self.backend.notify_vm(vm.uuid, vm.name, vm.port)

        logging.debug('%s:%s connected' % (vm.uuid, repr(vm.name)))
        if vm.port is not None:
            logging.debug("listening on port %d" % vm.port)

        # The clock is always ticking
        self.stamp_orphan(vm)

        return vm

    def _add_vm_when_ready(self, vt):
        if not vt.name or not vt.uuid:
            return

        self.new_vm(vt.uuid, vt.name, vts = [vt])

    def handle_vc_uuid(self, vt):
        if not self.vms.has_key(vt.uuid):
            self._add_vm_when_ready(vt)
            return

        # This could be a reconnect, or it could be a vmotion
        # peer. Regardless, it's easy enough just to allow this
        # new vt to send to all clients, and all clients to
        # receive.
        vm = self.vms[vt.uuid]
        vm.vts.append(vt)

        logging.debug('uuid %s VM reconnect, %d active' %
                      (vm.uuid, len(vm.vts)))

    def handle_vm_name(self, vt):
        if not self.vms.has_key(vt.uuid):
            self._add_vm_when_ready(vt)
            return

        vm = self.vms[vt.uuid]
        if vt.name != vm.name:
            vm.name = vt.name
            self.backend.notify_vm(vm.uuid, vm.name, vm.port)

    def handle_vmotion_begin(self, vt, data):
        if not vt.uuid:
            # No Vm structure created yet
            return False

        vm = self.vms[vt.uuid]
        if vm.vmotion:
            return False

        vm.vmotion = data
        self.vmotions[data] = vt.uuid

        return True

    def handle_vmotion_peer(self, vt, data):
        if not self.vmotions.has_key(data):
            logging.debug('peer cookie %s doesn\'t exist' % hexdump(data))
            return False

        logging.debug('peer cookie %s maps to uuid %s' %
                      (hexdump(data), self.vmotions[data]))

        peer_uuid = self.vmotions[data]
        if vt.uuid:
            vm = self.vms[vt.uuid]
            if vm.uuid != peer_uuid:
                logging.debug('peer uuid %s != other uuid %s' % hexdump(data))
                return False
            return True # vt already in place
        else:
            # Act like we just learned the uuid
            vt.uuid = peer_uuid
            self.handle_vc_uuid(vt)

        return True

    def handle_vmotion_complete(self, vt):
        logging.debug('uuid %s vmotion complete' % vt.uuid)
        vm = self.vms[vt.uuid]
        del self.vmotions[vm.vmotion]
        vm.vmotion = None

    def handle_vmotion_abort(self, vt):
        logging.debug('uuid %s vmotion abort' % vt.uuid)
        vm = self.vms[vt.uuid]
        if vm.vmotion:
            del self.vmotions[vm.vmotion]
            vm.vmotion = None

    def check_orphan(self, vm):
        return len(vm.vts) == 0 and len(vm.clients) == 0

    def stamp_orphan(self, vm):
        if self.check_orphan(vm):
            self.orphans.append(vm.uuid)
            vm.last_time = time.time()

    def new_admin_connection(self, sock):
        self.collect_orphans()
        self.backend.notify_query_socket(sock, self)

    def queue_new_admin_connection(self, listener):
        sock = listener.accept()[0]
        self.task_queue.put(lambda: self.new_admin_connection(sock))

    def new_admin_client_connection(self, sock, uuid, readonly):
        client = self.Client(sock)
        client.uuid = uuid

        vm = self.vms[uuid]

        if not readonly:
            self.add_reader(client, self.queue_new_client_data)
        vm.clients.append(client)

        logging.debug('uuid %s new client, %d active clients'
                      % (client.uuid, len(vm.clients)))

    def queue_new_admin_client_connection(self, sock, uuid, readonly):
        self.task_queue.put(lambda: self.new_admin_client_connection(sock, uuid, readonly))

    def collect_orphans(self):
        t = time.time()

        orphans = self.orphans[:]
        for uuid in orphans:
            if not self.vms.has_key(uuid):
                self.orphans.remove(uuid)
                continue
            vm = self.vms[uuid]

            if not self.check_orphan(vm):
                self.orphans.remove(uuid) # Orphan no longer
                continue
            elif vm.last_time + self.vm_expire_time > t:
                continue

            logging.debug('expired VM with uuid %s' % uuid)
            if vm.port is not None:
                logging.debug(", port %d" % vm.port)
            self.backend.notify_vm_del(vm.uuid)

            self.del_all(vm)
            del vm.listener
            if self.vm_port_next is not None:
                self.vm_port_next = min(vm.port, self.vm_port_next)
                del self.ports[vm.port]
            del self.vms[uuid]
            if vm.vmotion:
                del self.vmotions[vm.vmotion]
                vm.vmotion = None
            del vm

    def open_vm_port(self, vm, port):
        self.collect_orphans()

        if self.vm_port_next is None:
            return

        if port:
            vm.port = port
        else:
            p = self.vm_port_next
            while self.ports.has_key(p):
                p += 1

            self.vm_port_next = p + 1
            vm.port = p

        assert not self.ports.has_key(vm.port)
        self.ports[vm.port] = vm.uuid

        vm.listener = openport(vm.port)
        self.add_reader(vm, self.queue_new_client_connection)

    def create_old_vms(self, vms):
        for vm in vms:
            self.new_vm(uuid = vm.uuid, name = vm.name, port = vm.port)

    def run(self):
        logging.info('Starting vSPC on proxy port %d, admin port %d' %
                     (self.proxy_port, self.admin_port))
        if self.vm_port_next is not None:
            logging.info("Allocating VM ports starting at %d" % self.vm_port_next)

        self.create_old_vms(self.backend.get_observed_vms())

        self.add_reader(openport(self.proxy_port, self.do_ssl, self.ssl_cert, self.ssl_key), self.queue_new_vm_connection)
        self.add_reader(openport(self.admin_port), self.queue_new_admin_connection)
        self.start()
        self.run_forever()

class AdminProtocolClient(Poller):
    def __init__(self, host, admin_port, vm_name, src, dst, lock_mode):
        Poller.__init__(self)
        self.admin_port = admin_port
        self.host       = host
        self.vm_name    = vm_name
        # needed for the poller to work
        assert hasattr(src, "fileno")
        self.command_source = src
        self.destination    = dst
        self.lock_mode      = lock_mode

    class Client(TelnetServer):
        def __init__(self, sock,
                     server_opts = (BINARY, SGA, ECHO),
                     client_opts = (BINARY, SGA)):
            TelnetServer.__init__(self, sock, server_opts, client_opts)
            self.uuid = None

    def connect_to_vspc(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((self.host, self.admin_port))
        sockfile = s.makefile()

        unpickler = pickle.Unpickler(sockfile)

        # trade protocol versions
        pickle.dump(Q_VERS, sockfile)
        sockfile.flush()
        server_vers = int(unpickler.load())
        if server_vers == 2:
            pickle.dump(self.vm_name, sockfile)
            pickle.dump(self.lock_mode, sockfile)
            sockfile.flush()
            status = unpickler.load()
            if status == Q_VM_NOTFOUND:
                if self.vm_name is not None:
                    sys.stderr.write("The host '%s' couldn't find the vm '%s'. "
                                     "The host knows about the following VMs:\n" % (self.host, self.vm_name))
                vm_list = unpickler.load()
                self.process_noninteractive(vm_list)
                return None
            elif status == Q_LOCK_BAD:
                sys.stderr.write("The host doesn't understand how to give me a write lock\n")
                return None
            elif status == Q_LOCK_FAILED:
                sys.stderr.write("Someone else has a write lock on the VM\n")
                return None

            assert status == Q_OK
            applied_lock_mode = unpickler.load()
            if applied_lock_mode == Q_LOCK_FFAR:
                self.destination.write("Someone else has an exclusive write lock; operating in read-only mode\n")
            seed_data = unpickler.load()

            for entry in seed_data:
                self.destination.write(entry)

        elif server_vers == 1:
            vers, resp = unpickler.load()
            assert vers == server_vers
            self.process_noninteractive(resp)
            return None

        else:
            sys.stderr.write("Server sent us a version %d response, "
                             "which we don't understand. Bad!" % vers)
            return None

        # From this point on, we write data directly to s; the rest of
        # the protocol doesn't bother with pickle.
        client = self.Client(sock = s)
        return client

    def new_client_data(self, listener):
        """
        I'm called when we have new data to send to the vSPC.
        """
        data = listener.read()
        if CLIENT_ESCAPE_CHAR in data:
            loc = data.index(CLIENT_ESCAPE_CHAR)
            pre_data = data[:loc]
            self.send_buffered(self.vspc_socket, pre_data)
            post_data = data[loc+1:]
            data = self.process_escape_character() + post_data

        self.send_buffered(self.vspc_socket, data)

    def send_buffered(self, ts, s = ''):
        if ts.send_buffered(s):
            self.add_writer(ts, self.send_buffered)
        else:
            self.del_writer(ts)

    def new_server_data(self, client):
        """
        I'm called when the AdminProtocolClient gets new data from the vSPC.
        """
        neg_done = False
        try:
            neg_done = client.negotiation_done()
        except (EOFError, IOError, socket.error):
            self.quit()

        if not neg_done:
            return

        s = None
        try:
            s = client.read_very_lazy()
        except (EOFError, IOError, socket.error):
            self.quit()
        if not s: # May only be option data, or exception
            return

        while s:
            c = s[:100]
            s = s[100:]
            self.destination.write(c)

    def process_escape_character(self):
        self.restore_terminal()
        ret = ""
        # make sure the prompt shows up on its own line.
        self.destination.write("\n")
        while True:
            self.destination.write("vspc> ")
            c = self.command_source.readline()
            if c == "": # EOF
                c = "quit"
            c = c.strip()
            if c == "quit" or c == "q":
                self.quit()
            # treat enter/return as continue
            elif c == "continue" or c == "" or c == "c":
                break
            elif c == "print-escape":
                ret = CLIENT_ESCAPE_CHAR
                break
            else:
                help = ("quit:         exit the client\n"
                        "continue:     exit this menu\n"
                        "print-escape: send the escape sequence to the VM\n")
                self.destination.write(help)
        self.prepare_terminal()
        return ret

    def process_noninteractive(self, listing):
        if type(listing) == type(Exception()):
            sys.stderr.write("Server complained: %s\n" % str(listing))
            return

        assert isinstance(listing, list)
        # sort vms by name
        listing.sort(key=lambda x: x[Q_NAME])

        for vm in listing:
            out = "%s:%s" % (vm[Q_NAME], vm[Q_UUID])
            if vm[Q_PORT] is not None:
                out += ":%d" % vm[Q_PORT]
            print out

    def prepare_terminal(self):
        fd = self.command_source
        self.oldterm = termios.tcgetattr(fd)
        newattr = self.oldterm[:]
        # this is essentially cfmakeraw

        # input modes
        newattr[0] = newattr[0] & ~(termios.IGNBRK | termios.BRKINT | \
                                    termios.PARMRK | termios.ISTRIP | \
                                    termios.IGNCR | termios.ICRNL | \
                                    termios.IXON)
        # output modes
        newattr[1] = newattr[1] & ~termios.OPOST
        # local modes
        newattr[3] = newattr[3] & ~(termios.ECHO | termios.ECHONL | \
                                    termios.ICANON | termios.IEXTEN | termios.ISIG)
        # special characters
        newattr[2] = newattr[2] & ~(termios.CSIZE | termios.PARENB)
        newattr[2] = newattr[2] | termios.CS8

        termios.tcsetattr(fd, termios.TCSANOW, newattr)

        self.oldflags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, self.oldflags | os.O_NONBLOCK)

    def restore_terminal(self):
        fd = self.command_source
        termios.tcsetattr(fd, termios.TCSAFLUSH, self.oldterm)
        fcntl.fcntl(fd, fcntl.F_SETFL, self.oldflags)

    def quit(self):
        self.restore_terminal()
        self.destination.write("\n")
        self.vspc_socket.close()
        sys.exit(0)

    def run(self):
        s = self.connect_to_vspc()
        if s is None:
            return

        try:
            self.prepare_terminal()
            self.vspc_socket = s

            self.add_reader(self.vspc_socket, self.new_server_data)
            self.add_reader(self.command_source, self.new_client_data)
            self.run_forever()
        except Exception, e:
            sys.stderr.write("Caught exception %s, closing" % e)
        finally:
            self.quit()

def get_backend_type(shortname):
    name = "vSPCBackend" + shortname
    if globals().has_key(name):
        backend_type = globals()[name]
    else:
        try:
            module = __import__(name)
        except ImportError:
            print "No builtin backend type %s found, no appropriate class " \
                "file found (looking for %s.py)" % (shortname, name)
            sys.exit(1)

        try:
            backend_type = getattr(module, name)
        except AttributeError:
            print "Backend module %s loaded, but class %s not found" % (name, name)
            sys.exit(1)

    return backend_type
