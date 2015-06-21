# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 Erik van Widenfelt
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.
#

import magic
import os
import pytz
import pwd
import random
import socket
import string

from datetime import datetime
from paramiko import SSHClient
from paramiko.ssh_exception import BadHostKeyException, AuthenticationException
from scp import SCPClient
from watchdog.events import PatternMatchingEventHandler

from django.conf import settings
from django.utils import timezone

from .models import RemoteFolder, TX_SENT, History
from getresults_tx.folder_handlers import BaseFolderHandler

tz = pytz.timezone(settings.TIME_ZONE)


class BaseEventHandler(PatternMatchingEventHandler):
    """
        event.event_type
            'modified' | 'created' | 'moved' | 'deleted'
        event.is_directory
            True | False
        event.src_path
            path/to/observed/file
    """
    def __init__(self, hostname, timeout, patterns):
        self.hostname = hostname or 'localhost'
        self.timeout = timeout or 5.0
        self.user = pwd.getpwuid(os.getuid()).pw_name
        super(BaseEventHandler, self).__init__(
            patterns=patterns, ignore_directories=True)

    def process(self, event):
        print('{} {}'.format(event.event_type, event.src_path))
        print('Nothing to do.')

    def on_modified(self, event):
        self.process(event)

    def on_created(self, event):
        self.process(event)

    def on_deleted(self, event):
        self.process(event)

    def on_moved(self, event):
        self.process(event)

    def connect(self):
        """Returns an ssh instance."""
        ssh = SSHClient()
        ssh.load_system_host_keys()
        try:
            ssh.connect(
                self.hostname,
                timeout=self.timeout
            )
        except AuthenticationException as e:
            raise AuthenticationException(
                'Got {}. Add user {} to authorized_keys on host {}'.format(
                    e, self.user, self.hostname))
        except BadHostKeyException as e:
            raise BadHostKeyException(
                'Add server to known_hosts on host {}.'
                ' Got {}.'.format(e, self.hostname))
        return ssh


class RemoteFolderEventHandler(BaseEventHandler):

    folder_handler = BaseFolderHandler()

    def __init__(self, *args):
        super(RemoteFolderEventHandler, self).__init__(patterns=['*.*'], *args)
        self._destination_subdirs = {}

    def on_created(self, event):
        """Move added files to a remote host."""
        self.process_added(event)

    def on_modified(self, event):
        """Move added files to a remote host."""
        self.process_added(event)

    def process_added(self, event):
        print('{} {}'.format(event.event_type, event.src_path))
        ssh = self.connect()
        filename = event.src_path.split('/')[-1:][0]
        path = os.path.join(self.source_dir, filename)
        mime_type = magic.from_file(path, mime=True)
        if mime_type in self.mime_types or self.mime_types is None:
            with SCPClient(ssh.get_transport()) as scp:
                fileinfo, destination_dir = self.put(scp, filename, mime_type)
                if fileinfo:
                    if self.archive_dir:
                        fileinfo['archive_filename'] = self.archive_filename(filename)
                        self.update_history(fileinfo, TX_SENT, destination_dir, mime_type)
                        os.rename(path, os.path.join(self.archive_dir, fileinfo['archive_filename']))
                    else:
                        os.remove(path)

    def select_destination_dir(self, filename, mime_type):
        """Returns the full path of the destination folder.

        Return value can be a list or tuple as long as the first item
        is the destination_dir."""
        try:
            return self.folder_handler.select(self, filename, mime_type, self.destination_dir)
        except TypeError as e:
            if 'object is not callable' in str(e):
                return self.destination_dir
            else:
                raise

    @property
    def destination_subdirs(self):
        """Returns a dictionary of subfolders expected to exist in the destination_dir."""
        if not self._destination_subdirs.get(self.destination_dir):
            self._destination_subdirs = {self.destination_dir: {}}
            for remote_folder in RemoteFolder.objects.filter(base_path=self.destination_dir):
                fldr = self.remote_folder(self.destination_dir, remote_folder.folder)
                self._remote_subfolders[self.destination_dir].update({
                    remote_folder.name: fldr,
                    remote_folder.file_hint: fldr})
        return self._destination_subdirs

    def destination_subdir(self, key):
        """Returns the name of a destination_dir subfolder given a key.

        Key can be the folder name or the folder hint. See RemoteFolder model.
        """
        try:
            return self.destination_subdirs[key]
        except KeyError:
            return self.destination_dir

    def put(self, scp, filename, mime_type, destination=None):
        """Copies file to the destination path and
        archives if the archive_dir has been specified."""

        selection = self.select_destination_dir(filename, mime_type)
        if isinstance(selection, (list, tuple)):
            destination_dir = selection[0]
        else:
            destination_dir = selection
        source_filename = os.path.join(self.source_dir, filename)
        destination_filename = os.path.join(destination_dir, filename)
        if not os.path.isfile(source_filename):
            return None
        fileinfo = self.statinfo(self.source_dir, filename)
        try:
            scp.put(
                source_filename,
                destination_filename
            )
        except IsADirectoryError:
            fileinfo = None
        return fileinfo, selection

    def statinfo(self, path, filename):
        statinfo = os.stat(os.path.join(self.source_dir, filename))
        return {
            'path': path,
            'filename': filename,
            'size': statinfo.st_size,
            'timestamp': tz.localize(datetime.fromtimestamp(statinfo.st_mtime)),
        }

    def update_history(self, fileinfo, status, destination_dir, mime_type):
        try:
            destination_dir, folder_hint = destination_dir
        except ValueError:
            folder_hint = None
        try:
            remote_folder = destination_dir.split('/')[-1:][0]
        except AttributeError:
            remote_folder = 'default'
        history = History(
            hostname=socket.gethostname(),
            remote_hostname=self.hostname,
            path=self.source_dir,
            remote_path=self.destination_dir,
            remote_folder=remote_folder,
            remote_folder_hint=folder_hint,
            archive_path=self.archive_dir,
            filename=fileinfo['filename'],
            filesize=fileinfo['size'],
            filetimestamp=fileinfo['timestamp'],
            mime_type=mime_type,
            status=status,
            sent_datetime=timezone.now(),
            user=self.user,
        )
        if self.archive_dir:
            history.archive.name = 'archive/{}'.format(fileinfo['archive_filename'])
        history.save()

    def archive_filename(self, filename):
        suffix = ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(5))
        try:
            f, ext = filename.split('.')
        except ValueError:
            f, ext = filename, ''
        return '.'.join(['{}_{}'.format(f, suffix), ext])
