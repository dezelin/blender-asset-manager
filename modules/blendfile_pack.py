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

import blendfile_path_walker

TIMEIT = True

# ------------------
# Ensure module path
import os
import sys
path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modules"))
if path not in sys.path:
    sys.path.append(path)
del os, sys, path
# --------


def pack(blendfile_src, blendfile_dst, mode='FILE',
         paths_remap_relbase=None,
         deps_remap=None, paths_remap=None, paths_uuid=None,
         # yield reports
         report=None):
    """
    :param deps_remap: Store path deps_remap info as follows.
       {"file.blend": {"path_new": "path_old", ...}, ...}

    :type deps_remap: dict or None
    """

    # Internal details:
    # - we copy to a temp path before operating on the blend file
    #   so we can modify in-place.
    # - temp files are only created once, (if we never touched them before),
    #   this way, for linked libraries - a single blend file may be used
    #   multiple times, each access will apply new edits ontop of the old ones.
    # - we track which libs we have touched (using 'lib_visit' arg),
    #   this means that the same libs wont be touched many times to modify the same data
    #   also prevents cyclic loops from crashing.

    import os
    import shutil

    from bam_utils.system import colorize

    path_temp_files = set()
    path_copy_files = set()

    SUBDIR = b'data'
    TEMP_SUFFIX = b'@'

    if report is None:
        report = lambda msg: msg

    yield report("%s: %r...\n" % (colorize("\nscanning deps", color='bright_green'), blendfile_src))

    if TIMEIT:
        import time
        t = time.time()

    def temp_remap_cb(filepath, level):
        """
        Create temp files in the destination path.
        """
        filepath = blendfile_path_walker.utils.compatpath(filepath)

        if level == 0:
            filepath_tmp = os.path.join(base_dir_dst, os.path.basename(filepath)) + TEMP_SUFFIX
        else:
            filepath_tmp = os.path.join(base_dir_dst, SUBDIR, os.path.basename(filepath)) + TEMP_SUFFIX

        filepath_tmp = os.path.normpath(filepath_tmp)

        # only overwrite once (so we can write into a path already containing files)
        if filepath_tmp not in path_temp_files:
            shutil.copy(filepath, filepath_tmp)
            path_temp_files.add(filepath_tmp)
        return filepath_tmp

    # base_dir_src = os.path.dirname(blendfile_src)
    base_dir_dst = os.path.dirname(blendfile_dst)

    base_dir_dst_subdir = os.path.join(base_dir_dst, SUBDIR)
    if not os.path.exists(base_dir_dst_subdir):
        os.makedirs(base_dir_dst_subdir)

    lib_visit = {}
    fp_blend_basename_last = b''

    for fp, (rootdir, fp_blend_basename) in blendfile_path_walker.FilePath.visit_from_blend(
            blendfile_src,
            readonly=False,
            temp_remap_cb=temp_remap_cb,
            recursive=True,
            lib_visit=lib_visit,
            ):

        # we could pass this in!
        fp_blend = os.path.join(fp.basedir, fp_blend_basename)

        if fp_blend_basename_last != fp_blend_basename:
            yield report("  %s:       %s\n" % (colorize("blend", color='blue'), fp_blend))
            fp_blend_basename_last = fp_blend_basename

        # assume the path might be relative
        path_src_orig = fp.filepath
        path_rel = blendfile_path_walker.utils.compatpath(path_src_orig)
        path_base = path_rel.split(os.sep.encode('ascii'))[-1]
        path_src = blendfile_path_walker.utils.abspath(path_rel, fp.basedir)

        # rename in the blend
        path_dst = os.path.join(base_dir_dst_subdir, path_base)

        if fp.level == 0:
            path_dst_final = b"//" + os.path.join(SUBDIR, path_base)
        else:
            path_dst_final = b'//' + path_base

        fp.filepath = path_dst_final

        # add to copy-list
        # never copy libs (handled separately)
        if not isinstance(fp, blendfile_path_walker.FPElem_block_path) or fp.userdata[0].code != b'LI':
            path_copy_files.add((path_src, path_dst))

        if deps_remap is not None:
            # this needs to become JSON later... ugh, need to use strings
            deps_remap.setdefault(
                    fp_blend_basename.decode('utf-8'),
                    {})[path_dst_final.decode('utf-8')] = path_src_orig.decode('utf-8')

    del lib_visit, fp_blend_basename_last

    if TIMEIT:
        print("  Time: %.4f\n" % (time.time() - t))

    yield report(("%s: %d files\n") %
                 (colorize("\narchiving", color='bright_green'), len(path_copy_files) + 1))

    # handle deps_remap and file renaming
    if deps_remap is not None:
        blendfile_src_basename = os.path.basename(blendfile_src).decode('utf-8')
        blendfile_dst_basename = os.path.basename(blendfile_dst).decode('utf-8')

        if blendfile_src_basename != blendfile_dst_basename:
            if mode != 'ZIP':
                deps_remap[blendfile_dst_basename] = deps_remap[blendfile_src_basename]
                del deps_remap[blendfile_src_basename]
        del blendfile_src_basename, blendfile_dst_basename

    # store path mapping {dst: src}
    if paths_remap is not None:

        if paths_remap_relbase is not None:
            relbase = lambda fn: os.path.relpath(fn, paths_remap_relbase)
        else:
            relbase = lambda fn: fn

        for src, dst in path_copy_files:
            # TODO. relative to project-basepath
            paths_remap[os.path.relpath(dst, base_dir_dst).decode('utf-8')] = relbase(src).decode('utf-8')
        # main file XXX, should have better way!
        paths_remap[os.path.basename(blendfile_src).decode('utf-8')] = relbase(blendfile_src).decode('utf-8')

        del relbase

    if paths_uuid is not None:
        from bam_utils.system import sha1_from_file

        for src, dst in path_copy_files:
            paths_uuid[os.path.relpath(dst, base_dir_dst).decode('utf-8')] = sha1_from_file(src)
        # XXX, better way to store temp target
        blendfile_dst_tmp = temp_remap_cb(blendfile_src, 0)
        paths_uuid[os.path.basename(blendfile_src).decode('utf-8')] = sha1_from_file(blendfile_dst_tmp)

        # blend libs
        for dst in path_temp_files:
            k = os.path.relpath(dst[:-len(TEMP_SUFFIX)], base_dir_dst).decode('utf-8')
            if k not in paths_uuid:
                paths_uuid[k] = sha1_from_file(dst)
            del k

        del blendfile_dst_tmp
        del sha1_from_file

    # --------------------
    # Handle File Copy/Zip

    if mode == 'FILE':
        blendfile_dst_tmp = temp_remap_cb(blendfile_src, 0)

        shutil.move(blendfile_dst_tmp, blendfile_dst)
        path_temp_files.remove(blendfile_dst_tmp)

        # strip TEMP_SUFFIX
        for fn in path_temp_files:
            shutil.move(fn, fn[:-1])

        for src, dst in path_copy_files:
            assert(b'.blend' not in dst)

            if not os.path.exists(src):
                yield report("  %s: %r\n" % (colorize("source missing", color='red'), src))
            else:
                yield report("  %s: %r -> %r\n" % (colorize("copying", color='blue'), src, dst))
                shutil.copy(src, dst)

        yield report("  %s: %r\n" % (colorize("written", color='green'), blendfile_dst))

    elif mode == 'ZIP':
        import zipfile
        with zipfile.ZipFile(blendfile_dst.decode('utf-8'), 'w', zipfile.ZIP_DEFLATED) as zip_handle:
            for fn in path_temp_files:
                yield report("  %s: %r -> <archive>\n" % (colorize("copying", color='blue'), fn))
                zip_handle.write(fn.decode('utf-8'),
                          arcname=os.path.relpath(fn[:-1], base_dir_dst).decode('utf-8'))
                os.remove(fn)

            shutil.rmtree(base_dir_dst_subdir)

            for src, dst in path_copy_files:
                assert(b'.blend' not in dst)

                if not os.path.exists(src):
                    yield report("  %s: %r\n" % (colorize("source missing", color='red'), src))
                else:
                    yield report("  %s: %r -> <archive>\n" % (colorize("copying", color='blue'), src))
                    zip_handle.write(src.decode('utf-8'),
                              arcname=os.path.relpath(dst, base_dir_dst).decode('utf-8'))

        yield report("  %s: %r\n" % (colorize("written", color='green'), blendfile_dst))
    else:
        raise Exception("%s not a known mode" % mode)


def create_argparse():
    import os
    import argparse

    usage_text = (
        "Run this script to extract blend-files(s) to a destination path:" +
        os.path.basename(__file__) +
        "--input=FILE --output=FILE [options]")

    parser = argparse.ArgumentParser(description=usage_text)

    # for main_render() only, but validate args.
    parser.add_argument(
            "-i", "--input", dest="path_src", metavar='FILE', required=True,
            help="Input path(s) or a wildcard to glob many files")
    parser.add_argument(
            "-o", "--output", dest="path_dst", metavar='DIR', required=True,
            help="Output file or a directory when multiple inputs are passed")
    parser.add_argument(
            "-m", "--mode", dest="mode", metavar='MODE', required=False,
            choices=('FILE', 'ZIP'), default='FILE',
            help="Output file or a directory when multiple inputs are passed")
    parser.add_argument(
            "-r", "--deps_remap", dest="deps_remap", metavar='FILE',
            help="Write out the path mapping to a JSON file")
    parser.add_argument(
            "-s", "--paths_remap", dest="paths_remap", metavar='FILE',
            help="Write out the original paths to a JSON file")
    parser.add_argument(
            "-u", "--paths_uuid", dest="paths_uuid", metavar='FILE',
            help="Write out the original paths UUID to a JSON file")

    return parser


def main():
    import sys

    parser = create_argparse()
    args = parser.parse_args(sys.argv[1:])

    encoding = sys.getfilesystemencoding()

    deps_remap = {} if args.deps_remap else None
    paths_remap = {} if args.paths_remap else None
    paths_uuid = {} if args.paths_uuid else None

    for msg in pack(
            args.path_src.encode(encoding),
            args.path_dst.encode(encoding),
            args.mode,
            deps_remap=deps_remap,
            paths_remap=paths_remap,
            paths_uuid=paths_uuid,
            ):
        print(msg)

    def write_dict_as_json(fn, dct):
        with open(fn, 'w', encoding='utf-8') as f:
            import json
            json.dump(
                    dct, f, ensure_ascii=False,
                    check_circular=False,
                    # optional (pretty)
                    sort_keys=True, indent=4, separators=(',', ': '),
                    )

    if deps_remap is not None:
        write_dict_as_json(args.deps_remap, deps_remap)

    if paths_remap is not None:
        write_dict_as_json(args.paths_remap, paths_remap)

    if paths_uuid is not None:
        write_dict_as_json(args.paths_uuid, paths_uuid)

    del write_dict_as_json

if __name__ == "__main__":
    main()
