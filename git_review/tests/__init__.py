# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import os
import shutil
import stat
import struct
import sys

import fixtures
import requests
import testtools
from testtools import content

from git_review.tests import utils

if sys.version < '3':
    import urllib
    import urlparse
    urlparse = urlparse.urlparse
else:
    import urllib.parse
    import urllib.request
    urlparse = urllib.parse.urlparse


WAR_URL = 'https://gerrit-releases.storage.googleapis.com/gerrit-2.13.14.war'
# Update GOLDEN_SITE_VER for every change altering golden site, including
# WAR_URL changes. Set new value to something unique (just +1 it for example)
GOLDEN_SITE_VER = '5'


# NOTE(yorik-sar): This function needs to be a perfect hash function for
# existing test IDs. This is verified by running check_test_id_hashes script
# prior to running tests. Note that this doesn't imply any cryptographic
# requirements for underlying algorithm, so we can use weak hash here.
# Range of results for this function is limited by port numbers selection
# in _pick_gerrit_port_and_dir method (it can't be greater than 10000 now).
def _hash_test_id(test_id):
    if not isinstance(test_id, bytes):
        test_id = test_id.encode('utf-8')
    hash_ = hashlib.md5(test_id).digest()
    num = struct.unpack("=I", hash_[:4])[0]
    return num % 10000


class DirHelpers(object):
    def _dir(self, base, *args):
        """Creates directory name from base name and other parameters."""
        return os.path.join(getattr(self, base + '_dir'), *args)


class IsoEnvDir(DirHelpers, fixtures.Fixture):
    """Created isolated env and associated directories"""

    def __init__(self, base_dir=None):
        super(IsoEnvDir, self).setUp()

        # set up directories for isolation
        if base_dir:
            self.base_dir = base_dir
            self.temp_dir = self._dir('base', 'tmp')
            if not os.path.exists(self.temp_dir):
                os.mkdir(self.temp_dir)
            self.addCleanup(shutil.rmtree, self.temp_dir)
        else:
            self.temp_dir = self.useFixture(fixtures.TempDir()).path

        self.work_dir = self._dir('temp', 'work')
        self.home_dir = self._dir('temp', 'home')
        self.xdg_config_dir = self._dir('home', '.xdgconfig')
        for path in [self.home_dir, self.xdg_config_dir, self.work_dir]:
            if not os.path.exists(path):
                os.mkdir(path)

        # ensure user proxy conf doesn't interfere with tests
        self.useFixture(fixtures.EnvironmentVariable('no_proxy', '*'))
        self.useFixture(fixtures.EnvironmentVariable('NO_PROXY', '*'))

        # isolate tests from user and system git configuration
        self.useFixture(fixtures.EnvironmentVariable('HOME', self.home_dir))
        self.useFixture(
            fixtures.EnvironmentVariable('XDG_CONFIG_HOME',
                                         self.xdg_config_dir))
        self.useFixture(
            fixtures.EnvironmentVariable('GIT_CONFIG_NOSYSTEM', "1"))
        self.useFixture(
            fixtures.EnvironmentVariable('EMAIL', "you@example.com"))
        self.useFixture(
            fixtures.EnvironmentVariable('GIT_AUTHOR_NAME',
                                         "gitreview tester"))
        self.useFixture(
            fixtures.EnvironmentVariable('GIT_COMMITTER_NAME',
                                         "gitreview tester"))


class GerritHelpers(DirHelpers):

    def init_dirs(self):
        self.primary_dir = os.path.abspath(os.path.curdir)
        self.gerrit_dir = self._dir('primary', '.gerrit')
        self.gsite_dir = self._dir('gerrit', 'golden_site')
        self.gerrit_war = self._dir('gerrit', WAR_URL.split('/')[-1])

    def ensure_gerrit_war(self):
        # check if gerrit.war file exists in .gerrit directory
        if not os.path.exists(self.gerrit_dir):
            os.mkdir(self.gerrit_dir)

        if not os.path.exists(self.gerrit_war):
            print("Downloading Gerrit binary from %s..." % WAR_URL)
            resp = requests.get(WAR_URL)
            if resp.status_code != 200:
                raise RuntimeError("Problem requesting Gerrit war")
            utils.write_to_file(self.gerrit_war, resp.content)
            print("Saved to %s" % self.gerrit_war)

    def init_gerrit(self):
        """Run Gerrit from the war file and configure it."""
        golden_ver_file = self._dir('gsite', 'golden_ver')
        if os.path.exists(self.gsite_dir):
            if not os.path.exists(golden_ver_file):
                golden_ver = '0'
            else:
                with open(golden_ver_file) as f:
                    golden_ver = f.read().strip()
            if GOLDEN_SITE_VER != golden_ver:
                print("Existing golden site has version %s, removing..." %
                      golden_ver)
                shutil.rmtree(self.gsite_dir)
            else:
                print("Golden site of version %s already exists" %
                      GOLDEN_SITE_VER)
                return

        print("Creating a new golden site of version " + GOLDEN_SITE_VER)

        # initialize Gerrit in developer mode, and include the
        # download-commands plugin (needed by some tests which try to retrieve
        # by Change-Id string)
        utils.run_cmd('java', '-jar', self.gerrit_war,
                      'init', '-d', self.gsite_dir, '--dev',
                      '--batch', '--no-auto-start', '--install-plugin',
                      'download-commands')

        # pre-index so that it won't be triggered on start for each copy
        utils.run_cmd('java', '-jar', self.gerrit_war, 'reindex',
                      '-d', self.gsite_dir)

        # record our site version
        with open(golden_ver_file, 'w') as f:
            f.write(GOLDEN_SITE_VER)

        # create SSH public key
        utils.run_cmd('ssh-keygen', '-t', 'rsa', '-b', '4096', '-m', 'PEM',
                      '-f', self._dir('gsite', 'test_ssh_key'), '-N', '')

    def _run_gerrit_cli(self, command, *args):
        """SSH to gerrit Gerrit server and run command there."""
        return utils.run_cmd('ssh', '-p', str(self.gerrit_port),
                             'admin@' + self.gerrit_host, 'gerrit',
                             command, *args)

    def _run_git_review(self, *args, **kwargs):
        """Run git-review utility from source."""
        git_review = utils.run_cmd('which', 'git-review')
        kwargs.setdefault('chdir', self.test_dir)
        return utils.run_cmd(git_review, *args, **kwargs)


class BaseGitReviewTestCase(testtools.TestCase, GerritHelpers):
    """Base class for the git-review tests."""

    _remote = 'gerrit'

    @property
    def project_uri(self):
        return self.project_ssh_uri

    def setUp(self):
        """Configure testing environment.

        Prepare directory for the testing and clone test Git repository.
        Require Gerrit war file in the .gerrit directory to run Gerrit local.
        """
        super(BaseGitReviewTestCase, self).setUp()
        self.useFixture(fixtures.Timeout(5 * 60, True))

        # ensures git-review command runs in local mode (for functional tests)
        self.useFixture(
            fixtures.EnvironmentVariable('GITREVIEW_LOCAL_MODE', ''))

        self.init_dirs()
        ssh_addr, ssh_port, http_addr, http_port, self.site_dir = \
            self._pick_gerrit_port_and_dir()
        self.gerrit_host, self.gerrit_port = ssh_addr, ssh_port

        self.test_dir = self._dir('site', 'tmp', 'test_project')
        self.ssh_dir = self._dir('site', 'tmp', 'ssh')
        self.project_ssh_uri = (
            'ssh://admin@%s:%s/test/test_project.git' % (
                ssh_addr, ssh_port))
        self.project_http_uri = (
            'http://admin:secret@%s:%s/test/test_project.git' % (
                http_addr, http_port))

        self._run_gerrit(ssh_addr, ssh_port, http_addr, http_port)
        self._configure_ssh(ssh_addr, ssh_port)

        # Upload SSH key for test admin user
        with open(self._dir('gsite', 'test_ssh_key.pub'), 'rb') as pub_key_fd:
            pub_key = pub_key_fd.read().decode().strip()
        resp = requests.post(
            'http://%s:%s/a/accounts/self/sshkeys' % (http_addr, http_port),
            auth=requests.auth.HTTPDigestAuth('admin', 'secret'),
            headers={'Content-Type': 'text/plain'},
            data=pub_key)
        if resp.status_code != 201:
            raise RuntimeError(
                'SSH key upload failed: %s "%s"' % (resp, resp.text))

        # create Gerrit empty project
        self._run_gerrit_cli('create-project', 'test/test_project',
                             '--empty-commit')

        # setup isolated area to work under
        self.useFixture(IsoEnvDir(self.site_dir))

        # prepare repository for the testing
        self._run_git('clone', self.project_uri)
        utils.write_to_file(self._dir('test', 'test_file.txt'),
                            'test file created'.encode())
        self._create_gitreview_file()

        # push changes to the Gerrit
        self._run_git('add', '--all')
        self._run_git('commit', '-m', 'Test file and .gitreview added.')
        self._run_git('push', 'origin', 'master')
        # push a branch to gerrit
        self._run_git('checkout', '-b', 'testbranch')
        utils.write_to_file(self._dir('test', 'test_file.txt'),
                            'test file branched'.encode())
        self._create_gitreview_file(defaultbranch='testbranch')
        self._run_git('add', '--all')
        self._run_git('commit', '-m', 'Branched.')
        self._run_git('push', 'origin', 'testbranch')
        # cleanup
        shutil.rmtree(self.test_dir)

        # go to the just cloned test Git repository
        self._run_git('clone', self.project_uri)
        self.configure_gerrit_remote()
        self.addCleanup(shutil.rmtree, self.test_dir)

        # ensure user is configured for all tests
        self._configure_gitreview_username()

    def set_remote(self, uri):
        self._run_git('remote', 'set-url', self._remote, uri)

    def reset_remote(self):
        self._run_git('remote', 'rm', self._remote)

    def attach_on_exception(self, filename):
        @self.addOnException
        def attach_file(exc_info):
            if os.path.exists(filename):
                content.attach_file(self, filename)
            else:
                self.addDetail(os.path.basename(filename),
                               content.text_content('Not found'))

    def _run_git(self, command, *args):
        """Run git command using test git directory."""
        if command == 'clone':
            return utils.run_git(command, args[0], self._dir('test'))
        return utils.run_git('--git-dir=' + self._dir('test', '.git'),
                             '--work-tree=' + self._dir('test'),
                             command, *args)

    def _run_git_sub(self, command, *args):
        """Run git command using submodule of test git directory."""
        if command == 'init':
            utils.run_git('init', self._dir('test', 'sub'))
            self._simple_change_sub('submodule content', 'initial commit')
            utils.run_git('submodule', 'add', os.path.join('.', 'sub'),
                          chdir=self._dir('test'))
            return self._run_git('commit', '-m', 'add submodule')
        return utils.run_git('--git-dir=' + self._dir('test', 'sub', '.git'),
                             '--work-tree=' + self._dir('test', 'sub'),
                             command, *args)

    def _run_gerrit(self, ssh_addr, ssh_port, http_addr, http_port):
        # create a copy of site dir
        if os.path.exists(self.site_dir):
            shutil.rmtree(self.site_dir)
        shutil.copytree(self.gsite_dir, self.site_dir)
        self.addCleanup(shutil.rmtree, self.site_dir)
        # write config
        with open(self._dir('site', 'etc', 'gerrit.config'), 'w') as _conf:
            new_conf = utils.get_gerrit_conf(
                ssh_addr, ssh_port, http_addr, http_port)
            _conf.write(new_conf)

        # If test fails, attach Gerrit config and logs to the result
        self.attach_on_exception(self._dir('site', 'etc', 'gerrit.config'))
        for name in ['error_log', 'sshd_log', 'httpd_log']:
            self.attach_on_exception(self._dir('site', 'logs', name))

        # start Gerrit
        gerrit_sh = self._dir('site', 'bin', 'gerrit.sh')
        utils.run_cmd(gerrit_sh, 'start')
        self.addCleanup(utils.run_cmd, gerrit_sh, 'stop')

    def _unstaged_change(self, change_text, file_=None):
        """Helper method to create small changes and not stage them."""
        if file_ is None:
            file_ = self._dir('test', 'test_file.txt')
        utils.write_to_file(file_, ''.encode())
        self._run_git('add', file_)
        utils.write_to_file(file_, change_text.encode())

    def _uncommitted_change(self, change_text, file_=None):
        """Helper method to create small changes and not commit them."""
        if file_ is None:
            file_ = self._dir('test', 'test_file.txt')
        self._unstaged_change(change_text, file_)
        self._run_git('add', file_)

    def _simple_change(self, change_text, commit_message,
                       file_=None):
        """Helper method to create small changes and commit them."""
        self._uncommitted_change(change_text, file_)
        self._run_git('commit', '-m', commit_message)

    def _simple_amend(self, change_text, file_=None):
        """Helper method to amend existing commit with change."""
        if file_ is None:
            file_ = self._dir('test', 'test_file_new.txt')
        utils.write_to_file(file_, change_text.encode())
        self._run_git('add', file_)
        # cannot use --no-edit because it does not exist in older git
        message = self._run_git('log', '-1', '--format=%s\n\n%b')
        self._run_git('commit', '--amend', '-m', message)

    def _unstaged_change_sub(self, change_text, file_=None):
        """Helper method to create small submodule changes and not stage."""
        if file_ is None:
            file_ = self._dir('test', 'sub', 'test_file.txt')
        utils.write_to_file(file_, ''.encode())
        self._run_git_sub('add', file_)
        utils.write_to_file(file_, change_text.encode())

    def _uncommitted_change_sub(self, change_text, file_=None):
        """Helper method to create small submodule changes and not commit."""
        if file_ is None:
            file_ = self._dir('test', 'sub', 'test_file.txt')
        self._unstaged_change_sub(change_text, file_)
        self._run_git_sub('add', file_)

    def _simple_change_sub(self, change_text, commit_message, file_=None):
        """Helper method to create small submodule changes and commit them."""
        self._uncommitted_change_sub(change_text, file_)
        self._run_git_sub('commit', '-m', commit_message)

    def _configure_ssh(self, ssh_addr, ssh_port):
        """Setup ssh and scp to run with special options."""

        os.mkdir(self.ssh_dir)

        ssh_key = utils.run_cmd('ssh-keyscan', '-p', str(ssh_port), ssh_addr)
        utils.write_to_file(self._dir('ssh', 'known_hosts'), ssh_key.encode())
        self.addCleanup(os.remove, self._dir('ssh', 'known_hosts'))

        # Attach known_hosts to test results if anything fails
        self.attach_on_exception(self._dir('ssh', 'known_hosts'))

        for cmd in ('ssh', 'scp'):
            cmd_file = self._dir('ssh', cmd)
            s = '#!/bin/sh\n' \
                '/usr/bin/%s -i %s -o UserKnownHostsFile=%s ' \
                '-o IdentitiesOnly=yes ' \
                '-o PasswordAuthentication=no $@' % \
                (cmd,
                 self._dir('gsite', 'test_ssh_key'),
                 self._dir('ssh', 'known_hosts'))
            utils.write_to_file(cmd_file, s.encode())
            os.chmod(cmd_file, os.stat(cmd_file).st_mode | stat.S_IEXEC)

        os.environ['PATH'] = self.ssh_dir + os.pathsep + os.environ['PATH']
        os.environ['GIT_SSH'] = self._dir('ssh', 'ssh')

    def configure_gerrit_remote(self):
        self._run_git('remote', 'add', self._remote, self.project_uri)

    def _configure_gitreview_username(self):
        self._run_git('config', 'gitreview.username', 'admin')

    def _pick_gerrit_port_and_dir(self):
        hash_ = _hash_test_id(self.id())
        host = '127.0.0.1'
        return (
            host, 12000 + hash_,  # avoid 11211 that is memcached port on CI
            host, 22000 + hash_,  # avoid ephemeral ports at 32678+
            self._dir('gerrit', 'site-' + str(hash_)),
        )

    def _create_gitreview_file(self, **kwargs):
        cfg = ('[gerrit]\n'
               'scheme=%s\n'
               'host=%s\n'
               'port=%s\n'
               'project=test/test_project.git\n'
               '%s')
        parsed = urlparse(self.project_uri)
        host_port = parsed.netloc.rpartition('@')[-1]
        host, __, port = host_port.partition(':')
        extra = '\n'.join('%s=%s' % kv for kv in kwargs.items())
        cfg %= parsed.scheme, host, port, extra
        utils.write_to_file(self._dir('test', '.gitreview'), cfg.encode())


class HttpMixin(object):
    """HTTP remote_url mixin."""

    @property
    def project_uri(self):
        return self.project_http_uri

    def _configure_gitreview_username(self):
        # trick to set http password
        self._run_git('config', 'gitreview.username', 'admin:secret')
