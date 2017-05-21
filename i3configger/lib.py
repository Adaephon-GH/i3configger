import logging
import os
import subprocess
import time
import typing as t
from configparser import ConfigParser, NoOptionError
from pathlib import Path
from pprint import pformat

import itertools
from inotify import constants as ic
from inotify.adapters import Inotify

log = logging.getLogger(__name__)


class I3Configger:
    """Watch a directory tree and build/refresh/notify on changes"""
    MASK = (
        ic.IN_CREATE | ic.IN_ATTRIB | ic.IN_DELETE |
        ic.IN_MODIFY | ic.IN_CLOSE_WRITE |
        ic.IN_MOVED_FROM | ic.IN_MOVED_TO |
        ic.IN_DELETE_SELF | ic.IN_MOVE_SELF)
    """Tell inotify to trigger on changes"""

    def __init__(self, buildDefs, maxerrors=10):
        self.buildDefs: [BuildDef] = buildDefs
        self.allWatchPaths = itertools.chain.from_iterable(
            [bd.watchPaths for bd in self.buildDefs])
        self.maxerrors = maxerrors
        self.inotify_watcher = Inotify()
        self.lastBuild = None
        self.lastFilename = None
        self.errors = 0

    def build(self):
        for buildDef in self.buildDefs:
            buildDef.build()

    def watch(self):
        for event in self.get_events():
            self.process_event(event)

    def watch_guarded(self, func):
        for event in self.get_events():
            try:
                self.process_event(event)
            except:
                log.exception("I see dead calls ...")
                self.errors += 1
                if self.errors == self.maxerrors:
                    log.critical("%s errors occurred, crashing", self.errors)
                    raise RuntimeError("%s: giving up" % self)

    def get_events(self):
        for path in self.buildDefs.paths:
            self.inotify_watcher.add_watch(path, mask=self.MASK)
        for event in self.inotify_watcher.event_gen():
            if not event:
                continue
            yield event

    def get_event_data(self, event):
        (header, typeNames, watchPath, filename) = event
        watchPath = Path(watchPath.decode())
        filename = Path(filename.decode())
        log.debug("wd=%d mask=%d MASK->NAMES=%s "
                  "WATCH-PATH=[%s] FILENAME=[%s]",
                  header.wd, header.mask, typeNames,
                  watchPath, filename)
        return header, typeNames, watchPath, filename

    def process_event(self, event):
        header, typeNames, watchPath, filename = self.get_event_data(event)
        # todo handle IN_DELETE_SELF and IN_MOVE_SELF (error?)
        for buildDef in self.buildDefs:
            if buildDef.needs_build(header, typeNames, watchPath, filename):
                log.info("%s triggered build", filename)
                buildDef.build()
                IpcControl.notify_send('build %s', buildDef)
        IpcControl.restart_i3()


class BuildDef:
    BUILD_DELAY = 0.1
    """If an IDE does monkey business (e.g. Jetbrains "safe write")
    more than one change might be triggered for each change.

    Using a small delay to not trigger to often.
    """
    MARK: str = 'builddef:'
    """ini sections with this prefix are recognized as build definitions"""

    def __init__(self, config: ConfigParser, section):
        self.name: str = section.split(self.MARK)[-1]
        # mandatory settings
        self.target: Path = prep(config.get(section, 'target'))[0]
        self.sources: [Path] = prep(config.get(section, 'sources'))
        self.addheader: bool = config.getboolean(section, 'addheader')
        self.addinfo: bool = config.getboolean(section, 'addinfo')
        self.suffix = config.get(section, 'suffix')
        self.themes: [Path] = prep(config.get(section, 'themes'))
        # optional settings
        excludes = config.get(section, 'excludes', fallback=[])
        self.excludes: [Path] = prep(excludes, asPaths=False)
        files = config.get(section, 'files', fallback=[])
        self.files: [str] = prep(files, asPaths=False)
        try:
            self.theme: str = config.get(section, 'theme')
        except NoOptionError:
            self.theme = None
        # derived values and states
        self.watchPaths: [Path] = self.themes + self.sources
        self.lastFilename = None
        self.lastBuild = None

    def __str__(self):
        return pformat(vars(self))

    def __repr__(self):
        return str(self)

    def needs_build(self, header, typeNames, watchPath, filename) -> bool:
        if not self.matches(filename, watchPath):
            return False
        if not self.lastBuild:
            return True
        if filename != self.lastFilename:
            log.debug("%s != %s", filename, self.lastFilename)
            return True
        if time.time() - self.lastBuild >= self.BUILD_DELAY:
            return True
        log.debug("ignore %s changed too quick", filename)
        return False

    def matches(self, path):
        if path.suffix != self.suffix:
            return False
        # FIXME this is just a placeholder - likely wrong check
        if path.stem not in self.watchPaths:
            return False

    def build(self):
        out = []
        if self.addheader:
            msg = '# %s (i3configger: %s) #' % (self.name, time.asctime())
            sep = "#" * len(msg)
            out = ["%s\n%s\n%s" % (sep, msg, sep)]
        if self.theme:
            out.append(self.get_theme())
        for filePath in self.get_config_parts():
            content = filePath.read_text()
            if self.addinfo:
                info = "### %s ###\n" % filePath
                content = info + content
            out.append(content)
        self.target.write_text('\n'.join(out))
        log.debug("built %s from %s", self.target, self.sources)

    def get_theme(self):
        themePaths = []
        for themePath in self.themes:
            for tp in [p for p in themePath.iterdir()]:
                if not tp.is_file():
                    continue
                if tp.name == self.theme:
                    themePaths.append(tp)
        if not themePaths:
            raise BuildDefError("no content found for theme %s" % self.theme)
        content = '\n'.join([p.read_text() for p in themePaths])
        # TODO read variables and do interpolation
        return content

    def get_config_parts(self):
        """:returns: list of pathlib.Path"""
        sourcePaths = []
        for sourcePath in self.sources:
            for sp in [p for p in sourcePath.iterdir()]:
                if not sp.is_file():
                    continue
                if sp.name in self.excludes:
                    continue
                if sp.suffix != self.suffix:
                    continue
                if sp.name in self.excludes:
                    continue
                if self.files and sp.name not in self.files:
                    continue
                sourcePaths.append(sp)
        return sorted(sourcePaths)

    def check_sanity(self):
        if self.files and self.excludes:
            log.error("mutually exclusive keys used in %s", self)
            raise BuildDefError("files and excludes can't be used together.")

    # def as_bytes(self, watchPaths):
    #     return [p.encode() for p in watchPaths]


class BuildDefError(Exception):
    pass


class IpcControl:
    @classmethod
    def reload_i3(cls):
        if cls._send_i3_msg('reload'):
            cls.notify_send("reloaded i3")

    @classmethod
    def restart_i3(cls):
        if cls._send_i3_msg('restart'):
            cls.notify_send("restarted i3")

    @classmethod
    def _send_i3_msg(cls, msg):
        # todo use Adaephons i3 library
        cmd = ['i3', msg]
        output = subprocess.check_output(cmd).decode()
        if '"success": true' in output:
            return True
        cls.notify_send("%s failed: %s" % (cmd, output), urgency='critical')
        return False

    @classmethod
    def notify_send(cls, msg, urgency='low'):
        subprocess.check_call([
            'notify-send', '-a', 'i3configger', '-t', '1', '-u', urgency, msg])


class IniConfig:
    NAME = 'i3configger.ini'
    DEFAULT_PATH = Path(__file__).parent / NAME
    DEFAULT_USER_PATH = Path('~/.i3').expanduser() / 'config.d' / NAME

    def __init__(self, config: ConfigParser):
        self.buildDefs = self.make_build_defs(config)
        self.populate_settings(config)

    @staticmethod
    def make_build_defs(config: ConfigParser):
        buildDefs = []
        for section in config.sections():
            if not section.startswith(BuildDef.MARK):
                continue
            buildDefs.append(BuildDef(config, section))
        return buildDefs

    def populate_settings(self, config: ConfigParser):
        settings = config['settings']
        self.logfile = prep(settings['logfile'])[0]
        self.suffix = settings['suffix']
        self.maxerrors = settings.getint('maxerrors')

    @classmethod
    def get_config(cls, iniPath=None):
        config = ConfigParser()
        if iniPath:
            assert iniPath.exists(), iniPath
            path = iniPath
        else:
            if cls.DEFAULT_USER_PATH.exists():
                path = cls.DEFAULT_USER_PATH
            else:
                path = cls.DEFAULT_PATH
        config.read(path)
        return config


def prep(incoming: t.Union[str, t.List[str]], asPaths=True) \
        -> t.Union[t.List[Path], t.List[str]]:
    if not incoming:
        return []
    items = [incoming] if isinstance(incoming, str) else incoming
    serializedItems = []
    for item in items:
        if '\n' in item:
            serializedItems.extend(p for p in item.split('\n') if p)
        else:
            serializedItems.append(item)
    if not asPaths:
        return serializedItems
    expandedPaths = []
    for path in serializedItems:
        path = os.path.expandvars(path)
        path = Path(path).expanduser()
        expandedPaths.append(path)
    return expandedPaths
