#!/usr/bin/env python3

# ***** BEGIN GPL LICENSE BLOCK *****
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
# ***** END GPL LICENCE BLOCK *****

"""
Blender asset manager
"""


if __name__ != "__main__":
    raise Exception("must be imported directly")

# ------------------
# Ensure module path
import os
import sys
path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "modules"))
if path not in sys.path:
    sys.path.append(path)
del os, sys, path
# --------


class bam_config:
    # fake module
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        raise RuntimeError("%s should not be instantiated" % cls)

    CONFIG_DIR = ".bam"

    @staticmethod
    def find_basedir(cwd=None):
        """
        Return the config path (or None when not found)
        """
        import os

        if cwd is None:
            cwd = os.getcwd()

        parent = (os.path.normpath(
                  os.path.abspath(
                  cwd)))

        parent_prev = None

        while parent != parent_prev:
            test_dir = os.path.join(parent, bam_config.CONFIG_DIR)
            if os.path.isdir(test_dir):
                return test_dir

            parent_prev = parent
            parent = os.path.dirname(parent)

        return None

    @staticmethod
    def load(id_, cwd=None):
        basedir = bam_config.find_basedir(cwd=cwd)
        filepath = os.path.join(basedir, id_)

        with open(filepath, 'r') as f:
            import json
            return json.load(f)

    @staticmethod
    def write(id_, data, cwd=None):
        bam_config.find_basedir(cwd=cwd)
        filepath = os.path.join(basedir, id_)

        with open(filepath, 'w') as f:
            import json
            json.dump(
                    data, f, ensure_ascii=False,
                    check_circular=False,
                    # optional (pretty)
                    sort_keys=True, indent=4, separators=(',', ': '),
                    )


class bam_utils:
    # fake module
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        raise RuntimeError("%s should not be instantiated" % cls)

    @staticmethod
    def session_find_url():
        return "http://localhost:5000"

    @staticmethod
    def session_request_url(req_path):
        # TODO, get from config
        BAM_SERVER = bam_utils.session_find_url()
        result = "%s/%s" % (BAM_SERVER, req_path)
        return result

    @staticmethod
    def checkout(paths):
        import sys
        import requests

        # TODO(cam) multiple paths
        path = paths[0]
        del paths

        payload = {
            "filepath": path,
            "command": "checkout",
            }
        r = requests.get(
                bam_utils.session_request_url("file"),
                params=payload,
                auth=("bam", "bam"),
                stream=True,
                )

        if r.status_code not in {200, }:
            # TODO(cam), make into reusable function?
            print("Error %d:\n%s" % (r.status_code, next(r.iter_content(chunk_size=1024)).decode('utf-8')))
            return

        # TODO(cam) how to tell if we get back a message payload? or real data???
        local_filename = payload['filepath'].split('/')[-1]

        if 1:
            local_filename += ".zip"

        with open(local_filename, 'wb') as f:
            import struct
            ID_MESSAGE = 1
            ID_PAYLOAD = 2
            head = r.raw.read(4)
            if head != b'BAM\0':
                print("Bad header...")
                return

            while True:
                msg_type, msg_size = struct.unpack("<II", r.raw.read(8))
                if msg_type == ID_MESSAGE:
                    sys.stdout.write(r.raw.read(msg_size).decode('utf-8'))
                    sys.stdout.flush()
                elif msg_type == ID_PAYLOAD:
                    # payload
                    break

            tot_size = 0
            for chunk in r.iter_content(chunk_size=1024):
                if chunk: # filter out keep-alive new chunks
                    tot_size += len(chunk)
                    f.write(chunk)
                    f.flush()

                    sys.stdout.write("\rdownload: [%03d%%]" % ((100 * tot_size) // msg_size))
                    sys.stdout.flush()
        sys.stdout.write("\nwritten: %r\n" % local_filename)

    @staticmethod
    def commit(paths, message):
        import sys
        import os
        import requests

        # TODO(cam) ignore files

        # TODO(cam) multiple paths
        path = paths[0]

        if not os.path.isdir(path):
            print("Expected a directory (%r)" % path)
            sys.exit(1)

        # make a zipfile from session
        import json
        with open(os.path.join(path, ".bam_paths_uuid.json")) as f:
            paths_uuid = json.load(f)

        paths_modified = {}
        for fn, sha1 in paths_uuid.items():
            fn_abs = os.path.join(path, fn)
            if bam_utils.sha1_for_file(fn_abs) != sha1:
                paths_modified[fn] = fn_abs

        if not paths_modified:
            print("Nothing to commit!")
            return

        # -------------------------
        print("Now make a zipfile")
        import zipfile
        temp_zip = os.path.join(path, ".bam_tmp.zip")
        with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zip:
            for (fn, fn_abs) in paths_modified.items():
                print("  Archiving %r" % fn_abs)
                zip.write(fn_abs,
                          arcname=fn)

            # make a paths remap that only includes modified files
            # TODO(cam), from 'packer.py'
            def write_dict_as_json(fn, dct):
                zip.writestr(
                        fn,
                        json.dumps(dct,
                        check_circular=False,
                        # optional (pretty)
                        sort_keys=True, indent=4, separators=(',', ': '),
                        ).encode('utf-8'))

            with open(os.path.join(path, ".bam_paths_remap.json")) as f:
                paths_remap = json.load(f)

            paths_remap_subset = {k: v for k, v in paths_remap.items() if k in paths_modified}
            write_dict_as_json(".bam_paths_remap.json", paths_remap_subset)

        # --------------
        # Commit Request
        args = {
            'message': message,
            }
        payload = {
            'command': 'commit',
            'arguments': json.dumps(args),
            }
        files = {
            'file': open(temp_zip, 'rb'),
            }

        r = requests.put(
                bam_utils.session_request_url("file"),
                params=payload,
                auth=('bam', 'bam'),
                files=files)
        print("Return is:", r.text)

        files['file'].close()
        os.remove(temp_zip)

    @staticmethod
    def list_dir(paths):
        import sys
        import requests

        # TODO(cam) multiple paths
        path = paths[0]
        del paths

        payload = {
            "path": path,
            }
        r = requests.get(
                bam_utils.session_request_url("file_list"),
                params=payload,
                auth=("bam", "bam"),
                stream=True,
                )

        items = r.json().get("items_list", ())
        items.sort()

        for (name_short, name_full, file_type) in items:
            if file_type == "dir":
                print("  %s/" % name_short)
        for (name_short, name_full, file_type) in items:
            if file_type != "dir":
                print("  %s" % name_short)


def subcommand_checkout_cb(args):
    bam_utils.checkout(args.paths)


def subcommand_commit_cb(args):
    bam_utils.commit(args.paths, args.message)


def subcommand_update_cb(args):
    print(args)


def subcommand_revert_cb(args):
    print(args)


def subcommand_list_cb(args):
    bam_utils.list_dir(args.paths)


def subcommand_status_cb(args):
    print(args)


def create_argparse_checkout(subparsers):
    subparse = subparsers.add_parser("checkout", aliases=("co",))
    subparse.add_argument(
            "paths", nargs="+", help="Path(s) to operate on",
            )
    subparse.set_defaults(func=subcommand_checkout_cb)


def create_argparse_commit(subparsers):
    subparse = subparsers.add_parser("commit", aliases=("ci",))
    subparse.add_argument(
            "-m", "--message", dest="message", metavar='MESSAGE',
            help="Commit message",
            )
    subparse.add_argument(
            "paths", nargs="+", help="paths to commit",
            )

    subparse.set_defaults(func=subcommand_commit_cb)


def create_argparse_update(subparsers):
    subparse = subparsers.add_parser("update", aliases=("up",))
    subparse.add_argument(
            "paths", nargs="+", help="Path(s) to operate on",
            )
    subparse.set_defaults(func=subcommand_update_cb)


def create_argparse_revert(subparsers):
    subparse = subparsers.add_parser("revert", aliases=("rv",))
    subparse.add_argument(
            "paths", nargs="+", help="Path(s) to operate on",
            )
    subparse.set_defaults(func=subcommand_revert_cb)


def create_argparse_status(subparsers):
    subparse = subparsers.add_parser("status", aliases=("st",))
    subparse.add_argument(
            "paths", nargs="+", help="Path(s) to operate on",
            )
    subparse.set_defaults(func=subcommand_status_cb)


def create_argparse_list(subparsers):
    subparse = subparsers.add_parser("list", aliases=("ls",))
    subparse.add_argument(
            "paths", nargs="+", help="Path(s) to operate on",
            )
    subparse.set_defaults(func=subcommand_list_cb)


def create_argparse():
    import os
    import argparse

    usage_text = (
        "BAM! (Blender Asset Manager)\n" +
        __doc__
        )

    parser = argparse.ArgumentParser(description=usage_text)

    subparsers = parser.add_subparsers(
            title='subcommands',
            description='valid subcommands',
            help='additional help')

    create_argparse_checkout(subparsers)
    create_argparse_commit(subparsers)
    create_argparse_update(subparsers)
    create_argparse_revert(subparsers)
    create_argparse_status(subparsers)
    create_argparse_list(subparsers)

    return parser


def main():
    import sys

    parser = create_argparse()
    args = parser.parse_args(sys.argv[1:])

    # call subparser callback
    if not hasattr(args, "func"):
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()


