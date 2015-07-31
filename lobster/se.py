import glob
import multiprocessing
import os
import random
import re
import subprocess
import xml.dom.minidom

from contextlib import contextmanager
from functools import partial, wraps

import Chirp as chirp

logger = multiprocessing.get_logger()

class StorageElement(object):
    """Weird class to handle all needs of storage implementations.

    This class can be used for file system operations after at least one of
    its subclasses has been instantiated.

    "One size fits nobody." (T. Pratchett)
    """
    _defaults = []
    _systems = []

    def __init__(self, pfnprefix=None):
        """Create or use a StorageElement abstraction.

        As a user, use with no parameters to access various storage
        elements transparently.

        Subclasses should call the constructor with appropriate arguments,
        which should also be made available to the user.

        Parameters
        ----------
        pfnprefix : string, optional
            The path prefix under which relative file names can be
            accessed.
        """
        self.__master = False

        if pfnprefix is not None:
            self._pfnprefix = pfnprefix
            self._systems.append(self)
        else:
            self.__master = True

    def __getattr__(self, attr):
        if attr in self.__dict__ or not self.__master:
            return self.__dict__[attr]

        def switch(path=None):
            for imp in self._systems:
                try:
                    return imp.fixresult(getattr(imp, attr)(imp.lfn2pfn(path)))
                except:
                    pass
            raise AttributeError("no path resolution found for '{0}'".format(path))
        return switch

    def lfn2pfn(self, path):
        if path.startswith('/'):
            return os.path.join(self._pfnprefix, path[1:])
        return os.path.join(self._pfnprefix, path)

    def fixresult(self, res):
        def pfn2lfn(p):
            return p.replace(self._pfnprefix, '', 1)

        if isinstance(res, str):
            return pfn2lfn(res)

        try:
            return map(pfn2lfn, res)
        except TypeError:
            return res

    @classmethod
    def reset(cls):
        cls._systems = []

    @classmethod
    def store(cls):
        cls._defaults = list(cls._systems)

    @contextmanager
    def default(self):
        tmp = self._systems
        self._systems = self._defaults
        try:
            yield
        finally:
            self._systems = tmp

class Local(StorageElement):
    def __init__(self, pfnprefix=''):
        super(Local, self).__init__(pfnprefix)
        self.exists = os.path.exists
        self.getsize = os.path.getsize
        self.isdir = self._guard(os.path.isdir)
        self.isfile = self._guard(os.path.isfile)
        self.makedirs = os.makedirs
        self.remove = os.remove

    def _guard(self, method):
        """Protect method against non-existent paths.
        """
        def guarded(path):
            if not os.path.exists(path):
                raise IOError()
            return method(path)
        return guarded

    def ls(self, path):
        for fn in os.listdir(path):
            yield os.path.join(path, fn)

try:
    import hadoopy

    class Hadoop(StorageElement):
        def __init__(self, pfnprefix='/hadoop'):
            super(Hadoop, self).__init__(pfnprefix)

            self.exists = hadoopy.exists
            self.getsize = partial(hadoopy.stat, format='%b')
            self.isdir = hadoopy.isdir
            self.ls = hadoopy.ls
            self.makedirs = hadoopy.mkdir
            self.remove = hadoopy.rmr

            # local imports are not available after the module hack at the end
            # of the file
            self.__hadoop = hadoopy

        def isfile(self, path):
            return self.__hadoop.stat(path, '%F') == 'regular file'
except:
    pass

class Chirp(StorageElement):
    def __init__(self, server, pfnprefix):
        super(Chirp, self).__init__(pfnprefix)

        self.__c = chirp.Client(server, timeout=10)

    def exists(self, path):
        try:
            self.__c.stat(path)
            return True
        except IOError:
            return False

    def getsize(self, path):
        return self.__c.stat(path).size

    def isdir(self, path):
        return len(self.__c.ls(path)) > 0

    def isfile(self, path):
        return len(self.__c.ls(path)) == 0

    def ls(self, path):
        for f in self.__c.ls(path):
            if f.path not in ('.', '..'):
                yield os.path.join(path, f.path)

    def makedirs(self, path):
        self.__c.mkdir(path)

    def remove(self, path):
        self.__c.rm(path)

class SRM(StorageElement):
    def __init__(self, pfnprefix):
        super(SRM, self).__init__(pfnprefix)

        self.__stub = re.compile('^srm://[A-Za-z0-9:.\-/]+\?SFN=')

        # local imports are not available after the module hack at the end
        # of the file
        self.__sub = subprocess

    def execute(self, cmd, path, safe=False):
        cmds = cmd.split()
        args = ['lcg-' + cmds[0]] + cmds[1:] + ['-b', '-D', 'srmv2', path]
        try:
            p = self.__sub.Popen(args, stdout=self.__sub.PIPE, stderr=self.__sub.PIPE)
            p.wait()
            if p.returncode != 0 and not safe:
                msg = "Failed to execute '{0}':\n{1}\n{2}".format(' '.join(args), p.stderr.read(), p.stdout.read())
                raise IOError(msg)
        except OSError:
            raise AttributeError("srm utilities not available")
        return p.stdout.read()

    def strip(self, path):
        return self.__stub.sub('', path)

    def exists(self, path):
        try:
            self.execute('ls', path)
            return True
        except:
            return False

    def getsize(self, path):
        # FIXME this should be something meaningful in the future!
        return -666

    def isdir(self, path):
        return not self.isfile(path)

    def isfile(self, path):
        pre = self.__stub.match(path).group(0)
        output = self.execute('ls -l', path, True)
        if len(output.splitlines()) > 1:
            return False
        if output.startswith('d'):
            return False
        return True

    def ls(self, path):
        pre = self.__stub.match(path).group(0)
        for p in self.execute('ls', path).splitlines():
            yield pre + p

    def makedirs(self, path):
        return True

    def remove(self, path):
        # FIXME safe is active because SRM does not care about directories.
        self.execute('del', path, safe=True)

class StorageConfiguration(object):
    """Container for storage element configuration.
    """

    # Map protocol shorthands to actual protocol names
    __protocols = {
            'srm': 'srmv2',
            'root': 'xrootd'
    }

    # Matches CMS tiered computing site as found in
    # /cvmfs/cms.cern.ch/SITECONF/
    __site_re = re.compile(r'^T[0123]_(?:[A-Z]{2}_)?[A-Za-z0-9_\-]+$')
    # Breaks a URL down into 3 parts: the protocol, a optional server, and
    # the path
    __url_re = re.compile(r'^([a-z]+)://([^/]*)(.*)/?$')

    def __init__(self, config):
        self.__input = map(self._expand_site, config.get('input', []))
        self.__output = map(self._expand_site, config.get('output', []))

        self.__wq_inputs = config.get('use work queue for inputs', False)
        self.__wq_outputs = config.get('use work queue for outputs', False)

        self.__shuffle_inputs = config.get('shuffle inputs', False)
        self.__shuffle_outputs = config.get('shuffle outputs', False)

        self.__no_streaming = config.get('disable input streaming', False)

        logger.debug("using input location {0}".format(self.__input))
        logger.debug("using output location {0}".format(self.__output))

    def _find_match(self, protocol, site, path):
        """Extracts the LFN to PFN translation from the SITECONF.

        >>> StorageConfiguration({})._find_match('xrootd', 'T3_US_NotreDame', '/store/user/spam/ham/eggs')
        (u'/+store/(.*)', u'root://xrootd.unl.edu//store/\\\\1')
        """
        file = os.path.join('/cvmfs/cms.cern.ch/SITECONF', site, 'PhEDEx/storage.xml')
        doc = xml.dom.minidom.parse(file)

        for e in doc.getElementsByTagName("lfn-to-pfn"):
            if e.attributes["protocol"].value != protocol:
                continue
            if e.attributes.has_key('destination-match') and \
                    not re.match(e.attributes['destination-match'].value, site):
                continue
            if path and len(path) > 0 and \
                    e.attributes.has_key('path-match') and \
                    re.match(e.attributes['path-match'].value, path) is None:
                continue

            return e.attributes["path-match"].value, e.attributes["result"].value.replace('$1', r'\1')
        raise AttributeError("No match found for protocol {0} at site {1}, using {2}".format(protocol, site, path))

    def _expand_site(self, url):
        """Expands a CMS site label in a url to the corresponding server.

        >>> StorageConfiguration({})._expand_site('root://T3_US_NotreDame/store/user/spam/ham/eggs')
        u'root://xrootd.unl.edu//store/user/spam/ham/eggs'
        """
        protocol, server, path = self.__url_re.match(url).groups()

        if self.__site_re.match(server) and protocol in self.__protocols:
            regexp, result = self._find_match(self.__protocols[protocol], server, path)
            return re.sub(regexp, result, path)

        return "{0}://{1}{2}/".format(protocol, server, path)

    def transfer_inputs(self):
        """Indicates whether input files need to be transferred manually.
        """
        return self.__wq_inputs

    def transfer_outputs(self):
        """Indicates whether output files need to be transferred manually.
        """
        return self.__wq_outputs

    def local(self, filename):
        for url in self.__input + self.__output:
            protocol, server, path = self.__url_re.match(url).groups()

            if protocol != 'file':
                continue

            fn = os.path.join(path, filename)
            if os.path.isfile(fn):
                return fn
        raise IOError("Can't create LFN without local storage access")

    def _initialize(self, methods):
        for url in methods:
            protocol, server, path = self.__url_re.match(url).groups()

            if protocol == 'chirp':
                try:
                    Chirp(server, path)
                except chirp.AuthenticationFailure:
                    raise AttributeError("cannot access chirp server")
            elif protocol == 'file':
                Local(path)
            elif protocol == 'hdfs':
                try:
                    Hadoop(path)
                except NameError:
                    raise NotImplementedError("hadoop support is missing on this system")
            elif protocol == 'srm':
                SRM(url)
            else:
                logger.debug("implementation of master access missing for URL {0}".format(url))

    def activate(self):
        """Sets file system access methods.

        Replaces default file system access methods with the ones specified
        per configuration for input and output storage element access.
        """
        StorageElement.reset()

        self._initialize(self.__input)

        StorageElement.store()
        StorageElement.reset()

        self._initialize(self.__output)

    def preprocess(self, parameters, merge):
        """Adjust the storage transfer parameters sent with a task.

        Parameters
        ----------
        parameters : dict
            The task parameters to alter.  This method will add keys
            'input', 'output', and 'disable streaming'.
        merge : bool
            Specify if this is a merging parameter set.
        """
        if self.__shuffle_inputs:
            random.shuffle(self.__input)
        if self.__shuffle_outputs or (self.__shuffle_inputs and merge):
            random.shuffle(self.__output)

        parameters['input'] = self.__input if not merge else self.__output
        parameters['output'] = self.__output
        parameters['disable streaming'] = self.__no_streaming
