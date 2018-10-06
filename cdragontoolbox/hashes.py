import os
import sys
import re
import glob
import itertools
import signal
import time
import json
import logging
from contextlib import contextmanager
from typing import Dict
from xxhash import xxh64
from .data import REGIONS, Language

logger = logging.getLogger(__name__)


class HashFile:
    """Store hashes, support save/load and caching"""

    def __init__(self, filename, hash_size=16):
        self.filename = filename
        self.line_format = f"{{:0{hash_size}x}} {{}}"
        self.hashes = None

    def load(self, force=False) -> Dict[int, str]:
        if force or self.hashes is None:
            with open(self.filename) as f:
                hashes = (l.strip().split(' ', 1) for l in f)
                self.hashes = {int(h, 16): s for h, s in hashes}
        return self.hashes

    def save(self):
        with open(self.filename, 'w', newline='') as f:
            for h, s in sorted(self.hashes.items(), key=lambda kv: kv[1]):
                print(self.line_format.format(h, s), file=f)

hashfile_lcu = HashFile(os.path.join(os.path.dirname(__file__), "hashes.lcu.txt"))
hashfile_game = HashFile(os.path.join(os.path.dirname(__file__), "hashes.game.txt"))

def default_hashfile(path):
    """Return the default hashfile for the given WAD file"""
    if path.endswith(".wad.client"):
        return hashfile_game
    elif path.endswith(".wad"):
        return hashfile_lcu
    else:
        raise ValueError(f"no default hashes for WAD file '{path}'")


def build_wordlist(paths):
    """Build a list of words from paths"""

    words = set()
    re_split = re.compile(r'[/_.-]')
    for path in paths:
        words |= set(re_split.split(path)[:-1])

    # filter out numbers
    re_filter_words = re.compile(r'^[0-9]+$')
    words = set(w for w in words if not re_filter_words.search(w))
    return sorted(words)


@contextmanager
def sigint_callback(callback):
    """Context to display progress of long operations on SIGINT"""

    tlast = 0
    def signal_handler(signal, frame):
        nonlocal tlast
        callback()
        tnow = time.time()
        if tnow - tlast < 0.5:
            raise KeyboardInterrupt()
        tlast = tnow

    previous_handler = signal.signal(signal.SIGINT, signal_handler)
    try:
        yield
    finally:
        handler_back = signal.signal(signal.SIGINT, previous_handler)

def progress_iterate(sequence, formatter=None):
    """Iterate over sequence, display progress on SIGINT"""

    if formatter is None:
        formatter = lambda v: v

    interrupted = False
    def handler():
        nonlocal interrupted
        interrupted = True
    with sigint_callback(handler):
        for i, v in enumerate(sequence):
            if interrupted:
                print("  %5.1f%%  %s" % (100 * i / len(sequence), formatter(v)))
                interrupted = False
            yield v

def progress_iterator(sequence, formatter=None):
    # don't handle SIGINT if output is not a terminal
    if not os.isatty(sys.stderr.fileno()):
        return sequence
    return progress_iterate(sequence, formatter)



class HashGuesser:
    """
    Guess hashes from files
    """

    def __init__(self, hashfile, hashes):
        self.hashfile = hashfile
        if not isinstance(hashes, set):
            hashes = set(hashes)

        self.known = self.hashfile.load()
        self.unknown = hashes - set(self.known)
        self.wads = None
        self.__directory_list = None  # cache

    @classmethod
    def from_wads(cls, wads):
        """Create a guesser from wads"""
        hashes = set(wf.path_hash for wad in wads for wf in wad.files)
        self = cls(hashes)
        self.wads = wads
        return self

    @staticmethod
    def unknown_from_export(path):
        """Load unknown hashes from 'export/*.unknown.txt' files"""

        unknown = set()
        for p in glob.glob(f"{path}/*.unknown.txt"):
            with open(p) as f:
                unknown |= {int(h, 16) for h in f}
        return unknown

    def save(self):
        self.hashfile.save()

    def _add_known(self, h, p):
        print("%016x %s" % (h, p))
        self.known[h] = p
        self.unknown.remove(h)

    def check(self, p):
        """Check a single hash, print and add to known on match"""

        h = xxh64(p).intdigest()
        if h in self.unknown:
            self._add_known(h, p)

    def is_known(p):
        """Check a path, return True if it is known"""

        h = xxh64(p).intdigest()
        if h in self.unknown:
            self._add_known(h, p)
            return True
        return h in self.known

    def check_iter(self, paths):
        """Check paths from an iterable"""

        c = self.check
        for p in paths:
            c(p)

    def check_text_list(self, text):
        """Check paths from a text list"""
        self.check_iter(s for s in text.split() if s)

    def check_xdbg_hashes(self, path):
        with open(path) as f:
            self.check_iter(l.split('"')[1] for l in f if l.startswith('hash: '))

    def check_basenames(self, names):
        """Check a list of basenames for each known subdirectory"""

        self.check_iter(f"{d}/{name}" for d in self.directory_list() for name in names)

    def directory_list(self, cached=True):
        """Return a set of all directories and subdirectories"""

        if not cached or self.__directory_list is None:
            # do multiple passes to return intermediate directories
            dirs = set()
            bases = self.known.values()
            while len(bases):
                bases = {os.path.dirname(p) for p in bases} - dirs
                dirs |= bases
            self.__directory_list = dirs
        return self.__directory_list

    def substitute_basenames(self):
        """Check all basenames in each subdirectory"""

        names = set(os.path.basename(p) for p in self.known.values())
        dirs = self.directory_list()
        logger.info(f"substitute basenames: {len(names)} basenames, {len(dirs)} directories")
        for name in progress_iterator(sorted(names)):
            self.check_iter(f"{d}/{name}" for d in dirs)

    def _substitute_basename_words(self, words):
        """Replace all words in known basename"""

        re_extract = re.compile(r'([^/_.-]+)(?=[^/]*\.[^/]+$)')
        formats = set()
        for path in self.known.values():
            for m in re_extract.finditer(path):
                formats.add('%s%%s%s' % (path[:m.start()], path[m.end():]))

        logger.info(f"substitute basename words: {len(formats)} formats, {len(words)} words")
        for fmt in progress_iterator(sorted(formats)):
            self.check_iter(fmt % s for s in words)

    def _substitute_numbers(self, paths, nmax=10000, digits=None):
        """Guess hashes by changing numbers in basenames"""

        if digits is True:
            # guess digits count from nmax
            digits = len(str(nmax - 1))
        if digits is None:
            fmt = '%d'
            re_extract = re.compile(r'([0-9]+)(?=[^/]*\.[^/]+$)')
        else:
            fmt = f"%0{digits}d"
            re_extract = re.compile(r'([0-9]{%d})(?=[^/]*\.[^/]+$)' % digits)

        formats = set()
        for path in paths:
            for m in re_extract.finditer(path):
                formats.add(f'%s%s%s' % (path[:m.start()], fmt, path[m.end():]))

        nrange = range(nmax)
        logger.info(f"substitute numbers: {len(formats)} formats, nmax = {nmax}")
        for fmt in progress_iterator(sorted(formats)):
            self.check_iter(fmt % n for n in nrange)

    def substitute_extensions(self):
        """Guess hashes by substituting file extensions"""

        prefixes = set()
        extensions = set()
        for path in self.known.values():
            prefix, ext = os.path.splitext(path)
            prefixes.add(prefix)
            extensions.add(ext)

        logger.info(f"substitute extensions: {len(prefixes)} prefixes, {len(extensions)} extensions")
        for prefix in progress_iterator(sorted(prefixes)):
            self.check_iter(prefix + ext for ext in extensions)

    def guess_from_game_hashes(self):
        """Guess LCU hashes from game hashes"""

        base = 'plugins/rcp-be-lol-game-data/global/default'
        for path in hashfile_game.load().values():
            prefix, ext = os.path.splitext(path)
            if ext == '.dds':
                self.check(f"{base}/{prefix}.png")
                self.check(f"{base}/{prefix}.jpg")
            elif ext == '.json':
                self.check(f"{base}/{path}")

    def wad_text_files(self, wad):
        """Iterate over wad files, generate text file data"""

        with open(wad.path, 'rb') as f:
            for wadfile in wad.files:
                # skip non-text files as soon as possible
                if wadfile.ext in ('png', 'jpg', 'ttf', 'webm', 'ogg', 'dds', 'tga'):
                    continue
                try:
                    data = wadfile.read_data(f).decode('utf-8-sig')
                except UnicodeDecodeError:
                    continue
                if data:
                    yield wadfile, data


class LcuHashGuesser(HashGuesser):
    def __init__(self, hashes):
        super().__init__(hashfile_lcu, hashes)

    @classmethod
    def from_wads(cls, wads):
        """Create a guesser from wads

        WADs whose extension is not .wad are ignored.
        """
        return super().from_wads([wad for wad in wads if wad.path.endswith('.wad')])

    def build_wordlist(self):
        re_filter_path = re.compile(r"""'(?:
            ^(?:plugins/rcp-be-lol-game-data/global/default/v1/champion-ability-icons/
              | plugins/rcp-be-lol-game-data/global/default/data/characters/
              )
            | /[0-9a-f]{32}\.
            )""", re.VERBOSE)
        paths = (p for p in self.known.values() if not re_filter_path.search(p))
        return build_wordlist(paths)

    def substitute_region_lang(self):
        """Guess hashes from region/lang variants"""

        regions = REGIONS + ['global']
        langs = [l.value for l in Language] + ['default']

        regex = re.compile(r'^plugins/([^/]+)/[^/]+/[^/]+/')
        logger.info(f"substitute region and lang")
        region_lang_list = [(r, l) for r in regions for l in langs]
        known = list(self.known.values())
        for region_lang in progress_iterator(region_lang_list, lambda rl: f"{rl[0]}/{rl[1]}"):
            replacement = r'plugins/\1/%s/%s/' % region_lang
            self.check_iter(regex.sub(replacement, p) for p in known)

    def substitute_basename_words(self):
        super()._substitute_basename_words(self.build_wordlist())

    def substitute_numbers(self, nmax=10000, digits=None):
        re_filter = re.compile(r"""(?:
            ^(?:plugins/rcp-be-lol-game-data/[^/]+/[^/]+/v1/champion-
              | plugins/rcp-be-lol-game-data/global/default/(?:data|assets)/characters/
              | plugins/rcp-be-lol-game-data/global/default/data/items/icons2d/\d+_
              | plugins/rcp-be-lol-game-data/[^/]+/[^/]+/v1/champions/-1.json
              )
            | /[0-9a-f]{32}\.
            )""", re.VERBOSE)
        paths = (p for p in self.known.values() if not re_filter.search(p))
        super()._substitute_numbers(paths, nmax, digits)

    def substitute_plugin(self):
        """Guess hashes by changing plugin name"""

        all_paths = [p for p in self.known.values() if p.startswith('plugins/')]
        plugins = {p.split('/', 2)[1] for p in all_paths}
        formats = {re.sub(r'^plugins/([^/]+)/', r'plugins/%s/', p) for p in all_paths}

        logger.info(f"substitute plugin: {len(formats)} formats, {len(plugins)} plugins")
        for fmt in progress_iterator(sorted(formats)):
            self.check_iter(fmt % p for p in plugins)

    def grep_wad(self, wad):
        """Find hashes from a wad file"""

        logger.info(f"find LCU hashes in WAD {wad.path}")
        # candidate relative subpaths (not lowercased yet)
        relpaths = set()

        for wadfile, data in self.wad_text_files(wad):
            jdata = None
            if wadfile.ext == 'json':
                # parse specific information from known json files
                try:
                    jdata = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            if jdata is not None:
                if wadfile.path == 'plugins/rcp-fe-lol-loot/global/default/trans.json':
                    # names of hextech items, keys are also image names
                    self.check_iter(f"plugins/rcp-be-lol-game-data/global/default/v1/hextech-images/{k}.png" for k in jdata)
                    continue  # no more data to parse
                elif 'pluginDependencies' in jdata and 'name' in jdata:
                    # retrieve plugin name from description.json
                    # guess some common paths
                    name = jdata['name']
                    subpaths = ['index.html', 'init.js', 'init.js.map', 'bundle.js', 'trans.json', 'css/main.css', 'license.json']
                    self.check_iter(f"plugins/{name}/global/default/{subpath}" for subpath in subpaths)
                elif 'musicVolume' in jdata and 'files' in jdata:
                    # splash config
                    # try to guess subdirectory name (names should only contain one element)
                    names = {s for path in jdata['files'].values() for s in re.findall(r'-splash-([^.]+)', path)}
                    self.check_iter(f"plugins/rcp-fe-lol-splash/global/default/splash-assets/{name}/config.json" for name in names)
                    self.check_iter(f"plugins/rcp-fe-lol-splash/global/default/splash-assets/{name}/{path}" for name in names for path in jdata['files'].values())
                    continue  # no more data to parse
                elif wadfile.path == 'plugins/rcp-be-lol-game-data/global/default/v1/champion-summary.json':
                    champion_ids = [v['id'] for v in jdata]
                    self.check_iter(f'plugins/rcp-be-lol-game-data/global/default/v1/champions/{cid}.json' for cid in champion_ids)
                    self.check_iter(f'plugins/rcp-be-lol-game-data/global/default/v1/champion-splashes/{cid}/metadata.json' for cid in champion_ids)
                elif 'recommendedItemDefaults' in jdata:
                    # plugins/rcp-be-lol-game-data/global/default/v1/champions/{cid}.json
                    self.check_iter('plugins/rcp-be-lol-game-data/global/default' + p.lower() for p in jdata['recommendedItemDefaults'])

            # search for known paths formats
            # /fe/{plugin}/{subpath} -> plugins/rcp-fe-{plugin}/global/default/{subpath}
            self.check_iter(f"plugins/rcp-fe-{m.group(1)}/{m.group(2)}".lower()
                            for m in re.finditer(r"/fe/([^/]+)/([a-zA-Z0-9/_.@-]+)", data))
            # /DATA/{subpath} -> plugins/rcp-be-lol-game-data/global/default/data/{subpath}
            self.check_iter(f"plugins/rcp-be-lol-game-data/global/default/data/{m.group(1)}".lower()
                            for m in re.finditer(r"/DATA/([a-zA-Z0-9/_.@-]+)", data))
            # /lol-game-data/assets/{subpath} -> plugins/rcp-be-lol-game-data/global/default/{subpath}
            self.check_iter(f"plugins/rcp-be-lol-game-data/global/default/{m.group(1)}".lower()
                            for m in re.finditer(r'[/"]lol-game-data/assets/([a-zA-Z0-9/_.@-]+)', data))

            # relative path starting with ./ or ../ (e.g. require() use)
            relpaths |= {m.group(1) for m in re.finditer(r'[^a-zA-Z0-9/_.\\-]((?:\.|\.\.)/[a-zA-Z0-9/_.-]+)', data)}
            # basename or subpath (check for a extension)
            relpaths |= {m.group(1) for m in re.finditer(r'''["']([a-zA-Z0-9][a-zA-Z0-9/_.@-]*\.(?:js|json|webm|html|[a-z]{3}))\b''', data)}
            # template ID to template path
            relpaths |= {f"{m.group(1)}/template.html" for m in re.finditer(r'<template id="[^"]*-template-([^"]+)"', data)}
            # JS maps
            relpaths |= {m.group(1) for m in re.finditer(r'sourceMappingURL=(.*?\.js)\.map', data)}

        self.check_basenames(p.lower() for p in relpaths)

    def guess_patterns(self):
        """Guess from known path patterns"""

        # note: most values are already retrieved from json files in lol-game-data WAD

        # runes (perks)
        #XXX valid values, including 'p{i}_s{j}_k{k}.jpg' basenames could be retrieved from perkstyles.json
        perk_primary = range(8000, 8600, 100)
        for i in perk_primary:
            perk_secondary = range(i, i + 100)
            self.check_iter(f'plugins/rcp-fe-lol-perks/global/default/images/inventory-card/{i}/p{i}_s{j}_k{k}.jpg'
                            for j in [0] + list(perk_primary)
                            for k in [0] + list(perk_secondary)
                            )
            paths = ['environment.jpg', 'construct.png']
            paths += [f'keystones/{j}.png' for j in perk_secondary]
            paths += [f'second/{j}.png' for j in perk_primary]
            self.check_iter(f"plugins/rcp-fe-lol-perks/global/default/images/construct/{i}/{p}" for p in paths)

        # sanitizer
        langs = [l.value for l in Language]
        paths = []
        for i in range(5):
            for action in ('filter', 'unfilter'):
                paths += [f'{i}.{action}.csv']
                paths += [f'{i}.{action}.language.{x.split("_")[0]}.csv' for x in langs]
                paths += [f'{i}.{action}.country.{x.split("_")[1]}.csv' for x in langs]
                paths += [f'{i}.{action}.region.{x}c.csv' for x in REGIONS]
                paths += [f'{i}.{action}.locale.{x}.csv' for x in langs]
        for p in 'allowedchars breakingchars projectedchars projectedchars1337 punctuationchars variantaliases'.split():
            paths += [f'{p}.locale.{x}.txt' for x in langs]
            paths += [f'{p}.language.{x.split("_")[0]}.txt' for x in langs]
        self.check_iter(f'plugins/rcp-be-sanitizer/global/default/{p}' for p in paths)

        # plugins/rcp-fe-lol-loot/global/default/assets/loot_item_icons/{name}.png -> {name}_splash.png
        for p in self.known.values():
            if p.startswith('plugins/rcp-fe-lol-loot/global/default/assets/loot_item_icons/'):
                self.check(p.replace('.png', '_splash.png'))

        # patterns already checked when substituting numbers (and sometimes retrieved from WAD files)
        #  plugins/rcp-be-lol-game-data/global/default/content/src/leagueclient/wardskinimages/wardhero_{i}.png
        #  plugins/rcp-be-lol-game-data/global/default/content/src/leagueclient/wardskinimages/wardheroshadow_{i}.png
        #  plugins/rcp-be-lol-game-data/global/default/v1/profile-icons/{i}.jpg
        #  plugins/rcp-be-lol-game-data/global/default/v1/summoner-backdrops/{i}.jpg
        #  plugins/rcp-be-lol-game-data/global/default/v1/summoner-backdrops/{i}.webm
        #  plugins/rcp-fe-lol-loot/global/default/assets/loot_item_icons/chest_{i}.png
        #  plugins/rcp-fe-lol-loot/global/default/assets/loot_item_icons/chest_{i}_open.png
        #  plugins/rcp-fe-lol-loot/global/default/assets/loot_item_icons/material_{i}.png
        #  plugins/rcp-be-lol-game-data/global/default/v1/hextech-images/champion_skin_{i}.png
        #  plugins/rcp-be-lol-game-data/global/default/v1/hextech-images/champion_skin_rental_{i}.png
        #  plugins/rcp-fe-lol-skins-viewer/global/default/video/collection/{i}.webm


class GameHashGuesser(HashGuesser):
    def __init__(self, hashes):
        super().__init__(hashfile_game, hashes)

    @classmethod
    def from_wads(cls, wads):
        """Create a guesser from wads

        WADs whose extension is not .wad.client are ignored.
        """
        return super().from_wads([wad for wad in wads if wad.path.endswith('.wad.client')])

    def build_wordlist(self):
        return build_wordlist(self.known.values())

    def substitute_numbers(self, nmax=100, digits=None):
        paths = self.known.values()
        super()._substitute_numbers(paths, nmax, digits)

    def substitute_basename_words(self):
        super()._substitute_basename_words(self.build_wordlist())

    def substitute_character(self):
        """Guess hashes by changing champion names in assets/characters/"""

        characters = set()
        formats = set()
        re_char = re.compile(r'^assets/characters/([^/]+)/')
        for p in self.known.values():
            m = re_char.match(p)
            if not m:
                continue
            char = m.group(1)
            characters.add(char)
            formats.add(p.replace(char, '{}'))

        logger.info(f"substitute characters: {len(formats)} formats, {len(characters)} characters")
        for fmt in progress_iterator(sorted(formats)):
            self.check_iter(fmt.replace('{}', s) for s in characters)

    def substitute_skin_numbers(self):
        """Replace skinNN, multiple combinaisons"""

        characters = {}  # {char: ({skin}, {(format, N})}
        regex = re.compile(r'/characters/([^/]+)/skins/(base|skin\d+)/')
        for p in self.known.values():
            m = regex.search(p)
            if not m:
                continue
            char, skin = m.groups()
            if m.group(1) == 'sightward':
                continue
            c = characters.setdefault(char, (set(), set()))
            c[0].add(skin)
            c[1].add(re.subn(r'(?:base|skin\d+)', '%s', p))

        # generate all combinations
        logger.info(f"substitute skin numbers: {len(characters)} characters")
        for char, (skins, formats) in progress_iterator(characters.items(), lambda v: v[0]):
            for fmt, nocc in formats:
                self.check_iter(fmt % p for p in itertools.combinations(skins, nocc))

    def substitute_lang(self):
        """Guess hashes from lang variants"""

        langs = [l.value for l in Language]
        langs_re = re.compile(r'(%s)' % '|'.join(langs))
        formats = {langs_re.sub('{}', p) for p in self.known.values() if langs_re.search(p)}

        print(len(formats))
        logger.info(f"substitute lang: {len(formats)} formats, {len(langs)} langs")
        for fmt in progress_iterator(sorted(formats)):
            self.check_iter(fmt.replace('{}', l) for l in langs)

    def guess_skin_groups_bin(self):
        """Guess 'skin*.bin' with long filenames"""

        char_to_skins = {}
        regex = re.compile(r'^assets/characters/([^/]+)/skins/skin(\d+)/')
        for p in self.known.values():
            m = regex.match(p)
            if not m:
                continue
            char = m.group(1)
            nskin = int(m.group(2))
            if char == 'sightward':
                continue
            # always add skin 0 (base skin)
            char_to_skins.setdefault(char, {0}).add(nskin)

        # generate all combinations
        #TODO find a way to reduce number of combinations, for instance by always grouping chroma skinds together
        logger.info(f"find skin groups .bin files")
        for char, skins in progress_iterator(char_to_skins.items(), lambda v: v[0]):
            # note: skins are in lexicographic order: skin11 is before skin2
            str_skins = sorted(f"_skins_skin{i}" for i in skins)
            for n in range(len(str_skins)):
                for p in itertools.combinations(str_skins, n+1):
                    s = ''.join(p)
                    self.check(f"data/{char}{s}.bin")

    def guess_from_lcu_hashes(self):
        """Guess game hashes from LCU hashes"""

        re_data = re.compile(r"^plugins/rcp-be-lol-game-data/global/default/((?:assets|data)/.*)\.(png|jpg|json)$")
        for path in hashfile_lcu.load().values():
            m = re_data.match(path)
            if not m:
                continue
            prefix, ext = m.groups()
            if ext in ('png', 'jpg'):
                self.check(f"{prefix}.dds")
            else:
                self.check(f"{prefix}.{ext}")

    def grep_wad(self, wad):
        """Find hashes from a wad file"""

        logger.info(f"find game hashes in WAD {wad.path}")

        with open(wad.path, 'rb') as f:
            for wadfile in wad.files:
                if wadfile.ext in ('bin', 'inibin'):
                    # bin files: find strings based on prefix, then parse the length
                    data = wadfile.read_data(f)
                    for m in re.finditer(br'(..)((?:ASSETS|DATA)/[0-9a-zA-Z_. /-]+)', data):
                        n, path = m.groups()
                        n = n[0] + (n[1] << 8)
                        self.check(path[:n].lower().decode('ascii'))

                elif wadfile.ext == 'preload':
                    # preload files
                    data = wadfile.read_data(f)
                    fmt = os.path.dirname(wadfile.path) + '/%s.preload'
                    self.check_iter(fmt % m.group(1).lower().decode('ascii') for m in re.finditer(br'Name="([^"]+)"', data))

                elif wadfile.ext in ('hls', 'ps_2_0', 'ps_3_0', 'vs_2_0', 'vs_3_0'):
                    # shader: search for includes
                    data = wadfile.read_data(f)
                    dirname = os.path.dirname(wadfile.path)
                    for m in re.finditer(br'#include "([^"]+)"', data):
                        subpath = m.group(1).lower().decode('ascii')
                        self.check(os.path.normpath(f"{dirname}/{subpath}"))

                #TODO
                # .troybin: find .dds/.tga in footer
                # .fx: find .troy filenames
                # data/levels/project_clientlevelscripts/: guess .xml names from each other

    def grep_file(self, path):
        with open(path, "rb") as f:
            # find strings based on prefix, then parse the length
            for m in re.finditer(br'((?:ASSETS|DATA)/[0-9a-zA-Z_. /-]+)', f.read()):
                path = m.group(1)
                self.check(path.lower().decode('ascii'))

