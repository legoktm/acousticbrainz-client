# Copyright 2014 Music Technology Group - Universitat Pompeu Fabra
# acousticbrainz-client is available under the terms of the GNU
# General Public License, version 3 or higher. See COPYING for more details.

from __future__ import print_function

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import uuid

try:
    import requests
except ImportError:
    from .vendor import requests

from abz import compat, config

config.load_settings()


class AcousticBrainz:
    RESET = "\x1b[0m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"

    def __init__(self):
        self.conn = sqlite3.connect(config.get_sqlite_file())
        self.verbose = False
        self.session = requests.Session()

    def _update_progress(self, msg, status="...", colour=None):
        if colour is None:
            colour = self.RESET
        if self.verbose:
            sys.stdout.write("%s[%-10s]%s " % (colour, status, self.RESET))
            print(msg.encode("ascii", "ignore"))
        else:
            sys.stdout.write("%s[%-10s]%s " % (colour, status, self.RESET))
            sys.stdout.write(msg+"\x1b[K\r")
            sys.stdout.flush()

    def _start_progress(self, msg, status="...", colour=None):
        if colour is None:
            colour = self.RESET
        print()
        self._update_progress(msg, status, colour)

    def add_to_filelist(self, filepath, reason=None):
        query = """insert into filelog(filename, reason) values(?, ?)"""
        c = self.conn.cursor()
        c.execute(query, (compat.decode(filepath), reason))
        self.conn.commit()

    def is_valid_uuid(self, u):
        try:
            uuid.UUID(u)
            return True
        except ValueError:
            return False

    def get_status(self, filepath):
        """
        Get the status of the given filepath
        :param filepath:
        :return: basestring|bool
        """
        query = """select reason from filelog where filename = ?"""
        c = self.conn.cursor()
        r = c.execute(query, (compat.decode(filepath), ))
        rows = r.fetchall()
        if rows[0][0]:
            return rows[0][0]
        elif rows[0][0] is None:
            # Old style where reason is None
            return True
        else:
            return False

    def run_extractor(self, input_path, output_path):
        """
        :param input_path: path to the audio file
        :param output_path: path to a JSON file to write to
        :raises subprocess.CalledProcessError: if the extractor exits with a non-zero
                                               return code
        """
        extractor = config.settings["essentia_path"]
        profile = config.settings["profile_file"]
        args = [extractor, input_path, output_path, profile]

        p = subprocess.Popen(args, stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        (out, err) = p.communicate()
        retcode = p.returncode
        return retcode, out

    def submit_features(self, recordingid, features):
        featstr = json.dumps(features)

        host = config.settings["host"]
        url = compat.urlunparse(('http', host, '/%s/low-level' % recordingid, '', '', ''))
        r = self.session.post(url, data=featstr)
        r.raise_for_status()

    def process_features(self, filepath, features):
        """
        :param filepath: Path to file we're processing
        :param features: Output from the extractor that we want to submit
        :type features: dict
        :return: string status
        """
        trackids = features["metadata"]["tags"]["musicbrainz_trackid"]
        if not isinstance(trackids, list):
            trackids = [trackids]
        trs = [t for t in trackids if self.is_valid_uuid(t)]
        if trs:
            recid = trs[0]
            try:
                self.submit_features(recid, features)
            except requests.RequestException:  # Any general requests error
                self.add_to_filelist(filepath, "offline")
                self._update_progress(filepath, ":| offline", self.GREEN)
                return "offline"
            else:
                self.add_to_filelist(filepath, "done")
                self._update_progress(filepath, ":)", self.GREEN)
                return "done"
        else:
            self._update_progress(filepath, ":( badmbid", self.RED)
            return "badmbid"

    def handle_cached_result(self, filepath):
        """
        :param filepath: filename to check
        :return: bool
        """
        json_path = os.path.join(
            config.settings['cache_dir'],
            hashlib.md5(filepath.encode()).hexdigest() + '.json'
        )
        if not os.path.exists(json_path):
            return False
        with open(json_path) as f:
            features = json.load(f)
        self.process_features(filepath, features)
        return True

    def get_tmpname_for_file(self, filepath):
        """
        Get the JSON info filename given the audio file's name
        """
        return os.path.join(
            config.settings['cache_dir'],
            hashlib.md5(filepath.encode()).hexdigest() + '.json'
        )

    def process_file(self, filepath):
        """
        codec names from ffmpeg
        """
        self._start_progress(filepath)
        status = self.get_status(filepath)
        if status == 'offline':
            self.handle_cached_result(filepath)
            return
        elif status is True or status == 'done':
            self._update_progress(filepath, ":) done", self.GREEN)
            return
        elif status is not False:
            # Some error code
            self._update_progress(filepath, ":( %s" % status, self.RED)
            return

        tmpname = self.get_tmpname_for_file(filepath)
        if os.path.exists(tmpname):
            # This should have been caught earlier but...
            self.handle_cached_result(filepath)
            return
        retcode, out = self.run_extractor(filepath, tmpname)
        if retcode == 2:
            self._update_progress(filepath, ":( nombid", self.RED)
            print()
            print(out)
            self.add_to_filelist(filepath, "nombid")
        elif retcode == 1:
            self._update_progress(filepath, ":( extract", self.RED)
            print()
            print(out)
            self.add_to_filelist(filepath, "extractor")
        elif retcode > 0 or retcode < 0:  # Unknown error, not 0, 1, 2
            self._update_progress(filepath, ":( unk %s" % retcode, self.RED)
            print()
            print(out)
        else:
            if os.path.isfile(tmpname):
                try:
                    with open(tmpname) as f:
                        features = json.load(f)
                except ValueError:
                    self._update_progress(filepath, ":( json", self.RED)
                    self.add_to_filelist(filepath, "json")
                    return

                status = self.process_features(filepath, features)
                if status != "offline" and os.path.isfile(tmpname):
                    os.unlink(tmpname)

    def process_directory(self, directory_path):
        self._start_progress("processing %s" % directory_path)

        for dirpath, dirnames, filenames in os.walk(directory_path):
            for f in filenames:
                if f.lower().endswith(config.settings["extensions"]):
                    self.process_file(os.path.abspath(os.path.join(dirpath, f)))

    def process(self, path):
        if not os.path.exists(path):
            sys.exit(path + "does not exist")
        path = os.path.abspath(path)
        if os.path.isfile(path):
            self.process_file(path)
        elif os.path.isdir(path):
            self.process_directory(path)


def cleanup():
    if os.path.isfile(config.settings["profile_file"]):
        os.unlink(config.settings["profile_file"])
