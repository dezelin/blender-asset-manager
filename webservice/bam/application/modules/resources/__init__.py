import os
import json
import svn.local
import werkzeug
import xml.etree.ElementTree
import logging
from multiprocessing import Process

from flask import Flask
from flask import jsonify
from flask import abort
from flask import request
from flask import make_response
from flask import url_for
from flask import Response

from flask.ext.restful import Api
from flask.ext.restful import Resource
from flask.ext.restful import reqparse
from flask.ext.restful import fields
from flask.ext.restful import marshal

from application import auth
from application import app
from application import log
from application import db

from application.modules.admin import backend
from application.modules.admin import settings
from application.modules.projects import admin
from application.modules.projects.model import Project
from application.modules.projects.model import ProjectSetting
from application.modules.resources.model import Bundle


class DirectoryAPI(Resource):
    """Displays list of files."""

    decorators = [auth.login_required]

    def __init__(self):
        parser = reqparse.RequestParser()
        # parser.add_argument('rate', type=int, help='Rate cannot be converted')
        parser.add_argument('path', type=str)
        # args = parser.parse_args()
        super(DirectoryAPI, self).__init__()

    def get(self, project_name):

        project = Project.query.filter_by(name=project_name).first()

        path = request.args['path']
        if not path:
            path = ''

        path_root_abs = project.repository_path
        parent_path = ''

        if path != '':
            path_root_abs = os.path.join(path_root_abs, path)
            parent_path = os.pardir

        if not os.path.isdir(path_root_abs):
            return jsonify(message="Path is not a directory %r" % path_root_abs)

        items_list = []

        for f in os.listdir(path_root_abs):

            # ignore svn internal paths
            if f == ".svn":
                continue

            f_rel = os.path.join(path, f)
            f_abs = os.path.join(path_root_abs, f)

            if os.path.isdir(f_abs):
                items_list.append((f, f_rel, "dir"))
            else:
                items_list.append((f, f_rel, "file"))

        project_files = {
            "parent_path": parent_path,
            "items_list": items_list,
            }

        return jsonify(project_files)
        # return {'message': 'Display files list'}


class FileAPI(Resource):
    """Gives acces to a file. Currently requires 2 arguments:
    - filepath: the path of the file (relative to the project root)
    - the command (info, checkout)

    In the case of checkout we plan to support the following arguments:
    --dependencies
    --zip (eventually with a compression rate)

    Default behavior for file checkout is to retunr a zipfile with all dependencies.
    """

    decorators = [auth.login_required]

    def __init__(self):
        parser = reqparse.RequestParser()
        parser.add_argument('filepath', type=str,
            help="Filepath cannot be blank!")
        parser.add_argument('command', type=str, required=True,
            help="Command cannot be blank!")
        parser.add_argument('arguments', type=str)
        parser.add_argument('files', type=werkzeug.datastructures.FileStorage,
            location='files')
        # args = parser.parse_args()

        super(FileAPI, self).__init__()

    def get(self, project_name):
        command = request.args['command']
        command_args = request.args.get('arguments')
        if command_args is not None:
            command_args = json.loads(command_args)

        project = Project.query.filter_by(name=project_name).first()

        if command == 'info':
            filepath = request.args['filepath']

            r = svn.local.LocalClient(project.repository_path)
            svn_log = r.log_default(None, None, 5, filepath)
            svn_log = [l for l in svn_log]

            size = os.path.getsize(os.path.join(r.path, filepath))

            # Check bundle_status: (ready, in_progress)
            full_filepath = os.path.join(project.repository_path, filepath)
            b = Bundle.query.filter_by(source_file_path=full_filepath).first()
            if b:
                bundle_status = b.status
            else:
                bundle_status = None

            return jsonify(
                filepath=filepath,
                log=svn_log,
                size=size,
                bundle_status=bundle_status)

        elif command == 'bundle':
            filepath = request.args['filepath']
            #return jsonify(filepath=filepath, status="building")
            filepath = os.path.join(project.repository_path, filepath)

            if not os.path.exists(filepath):
                return jsonify(message="Path not found %r" % filepath)
            elif os.path.isdir(filepath):
                return jsonify(message="Path is a directory %r" % filepath)

            def bundle():
                def report(txt):
                    pass

                # pack the file!
                import tempfile

                # weak! (ignore original opened file)
                filepath_zip = tempfile.mkstemp(dir=app.config['STORAGE_BUNDLES'], suffix=".zip")
                os.close(filepath_zip[0])
                filepath_zip = filepath_zip[1]

                # subprocess here
                for r in self.pack_fn(
                        filepath, filepath_zip,
                        project.repository_path,
                        True,
                        report,
                        'ZIP',
                        ):
                    pass

                b = Bundle.query.filter_by(source_file_path=filepath).first()
                if b:
                    b.bundle_path = filepath_zip
                else:
                    b = Bundle(
                        source_file_path=filepath,
                        bundle_path=filepath_zip)
                    db.session.add(b)
                b.status = "available"
                db.session.commit()
                # once done, we update the queue, as well as the status of the
                # bundle in the table and serve the bundle_path
                # return jsonify(filepath=filepath_zip)


            # Check in database if file has been requested already
            b = Bundle.query.filter_by(source_file_path=filepath).first()
            if b:
                if b.status == "available":
                    # Check if archive is available on the filesystem
                    if os.path.isfile(b.bundle_path):
                        # serve the local path for the zip file
                        return jsonify(filepath=b.bundle_path, status="available")
                    else:
                        b.status = "building"
                        db.session.commit()
                        # build the bundle again
                elif b.status == "building":
                    # we are waiting for the server to build the archive
                    filepath=None
                    return jsonify(filepath=filepath, status="building")

            # If file not avaliable, start the bundling and return a None filepath,
            # which the cloud will interpret as, no file is available at the moment

            p = Process(target=bundle,)
            p.start()

            filepath=None
            return jsonify(filepath=filepath, status="building")

        elif command == 'checkout':
            filepath = request.args['filepath']
            filepath = os.path.join(project.repository_path, filepath)

            if not os.path.exists(filepath):
                return jsonify(message="Path not found %r" % filepath)
            elif os.path.isdir(filepath):
                return jsonify(message="Path is a directory %r" % filepath)

            def response_message_iter():
                ID_MESSAGE = 1
                ID_PAYLOAD = 2
                import struct

                def report(txt):
                    txt_bytes = txt.encode('utf-8')
                    return struct.pack('<II', ID_MESSAGE, len(txt_bytes)) + txt_bytes

                yield b'BAM\0'

                # pack the file!
                import tempfile

                # weak! (ignore original opened file)
                filepath_zip = tempfile.mkstemp(suffix=".zip")
                os.close(filepath_zip[0])
                filepath_zip = filepath_zip[1]

                yield from self.pack_fn(
                        filepath, filepath_zip,
                        project.repository_path,
                        command_args['all_deps'],
                        report,
                        # we don't infact pack any files here,
                        # only return a list of files we _would_ pack.
                        # see: checkout_download
                        'NONE',
                        )

                # TODO, handle fail
                if not os.path.exists(filepath_zip):
                    yield report("%s: %r\n" % (colorize("failed to extract", color='red'), filepath))
                    return

                with open(filepath_zip, 'rb') as f:
                    f.seek(0, os.SEEK_END)
                    f_size = f.tell()
                    f.seek(0, os.SEEK_SET)

                    yield struct.pack('<II', ID_PAYLOAD, f_size)
                    while True:
                        data = f.read(1024)
                        if not data:
                            break
                        yield data

            # return Response(f, direct_passthrough=True)
            return Response(response_message_iter(), direct_passthrough=True)
        elif command == 'checkout_download':
            # 4mb chunks
            CHUNK_COMPRESS = 4194304
            # CHUNK_COMPRESS = 512  # for testing, we can ensure many chunks are supported
            files = command_args['files']

            def response_message_iter():
                ID_MESSAGE = 1
                ID_PAYLOAD = 2
                ID_PAYLOAD_APPEND = 3
                ID_PAYLOAD_EMPTY = 4
                ID_DONE = 5
                import struct

                def report(txt):
                    txt_bytes = txt.encode('utf-8')
                    return struct.pack('<II', ID_MESSAGE, len(txt_bytes)) + txt_bytes

                yield b'BAM\0'

                # pack the file!
                for f_rel in files:
                    f_abs = os.path.join(project.repository_path, f_rel)
                    if os.path.exists(f_abs):
                        yield report("%s: %r\n" % ("downloading", f_rel))
                        # send over files
                        with open(f_abs, 'rb') as f:
                            f.seek(0, os.SEEK_END)
                            f_size = f.tell()
                            f.seek(0, os.SEEK_SET)

                            id_payload = ID_PAYLOAD

                            f_size_left = f_size
                            import lzma
                            while f_size_left:
                                data_raw = f.read(CHUNK_COMPRESS)
                                f_size_left -= len(data_raw)
                                data_lzma = lzma.compress(data_raw)
                                del data_raw
                                assert(f_size_left >= 0)

                                yield struct.pack('<II', id_payload, len(data_lzma))
                                yield data_lzma
                                id_payload = ID_PAYLOAD_APPEND
                    else:
                        yield report("%s: %r\n" % ("source missing", f_rel))
                        yield struct.pack('<II', ID_PAYLOAD_EMPTY, 0)


                yield struct.pack('<II', ID_DONE, 0)

            return Response(response_message_iter(), direct_passthrough=True)

        else:
            return jsonify(message="Command unknown")

    def put(self, project_name):
        project = Project.query.filter_by(name=project_name).first()
        command = request.args['command']
        command_args = request.args.get('arguments')
        if command_args is not None:
            command_args = json.loads(command_args)
        file = request.files['file']

        # Get the value of the first (and only) result for the specified project setting
        svn_password = next((setting.value
            for setting in project.settings
            if setting.name == 'svn_password'))
        svn_default_user = next((setting.value
            for setting in project.settings
            if setting.name == 'svn_default_user'))

        # We get the actual username from the http headers
        svn_user = auth.username()

        # If the setting does not exist, stop here and prevent any other operation
        if not svn_password:
            return make_response(jsonify(
                {'message': 'SVN missing password settings'}), 500)

        if file and self.allowed_file(file.filename):
            os.makedirs(project.upload_path, exist_ok=True)

            local_client = svn.local.LocalClient(project.repository_path)
            # TODO, add the merge operation to a queue. Later on, the request could stop here
            # and all the next steps could be done in another loop, or triggered again via
            # another request
            filename = werkzeug.secure_filename(file.filename)
            tmp_filepath = os.path.join(project.upload_path, filename)
            file.save(tmp_filepath)

            # TODO, once all files are uploaded, unpack and run the tasklist (copy, add, remove
            # files on a filesystem level and subsequently as svn commands)
            import zipfile

            extract_tmp_dir = os.path.splitext(tmp_filepath)[0]
            with open(tmp_filepath, 'rb') as zip_file:
                zip_handle = zipfile.ZipFile(zip_file)
                zip_handle.extractall(extract_tmp_dir)
            del zip_file, zip_handle
            del zipfile

            with open(os.path.join(extract_tmp_dir, '.bam_paths_remap.json'), 'r') as path_remap:
                path_remap = json.load(path_remap)

            import shutil
            for src_file_path, dst_file_path in path_remap.items():
                assert(os.path.exists(os.path.join(extract_tmp_dir, src_file_path)))

                src_file_path_abs = os.path.join(extract_tmp_dir, src_file_path)
                dst_file_path_abs = os.path.join(project.repository_path, dst_file_path)

                os.makedirs(os.path.dirname(dst_file_path_abs), exist_ok=True)

                shutil.move(src_file_path_abs, dst_file_path_abs)

            # TODO, dry run commit (using commit message)
            # Seems not easily possible with SVN, so we might just smartly use svn status
            result = local_client.run_command('status',
                [local_client.info()['entry_path'], '--xml'],
                combine=True)

            # We parse the svn status xml output
            root = xml.etree.ElementTree.fromstring(result)

            # Loop throught every entry reported by the svn status command
            for e in root.iter('entry'):
                file_path = e.attrib['path']
                item_status = e.find('wc-status').attrib['item']

                # We add each unversioned file to SVN
                if item_status == 'unversioned':
                    result = local_client.run_command('add',
                        [file_path, ])

            with open(os.path.join(extract_tmp_dir, '.bam_paths_ops.json'), 'r') as path_ops:
                path_ops = json.load(path_ops)

            log.debug(path_ops)
            for file_path, operation in path_ops.items():
                # TODO(fsiddi), collect all file paths and remove after
                if operation == 'D':
                    file_path_abs = os.path.join(project.repository_path, file_path)
                    assert(os.path.exists(file_path_abs))
                    result = local_client.run_command('rm',
                        [file_path_abs, ])

            # Commit command
            result = local_client.run_command('commit',
                [local_client.info()['entry_path'],
                '--no-auth-cache',
                '--message', command_args['message'],
                '--username', svn_user,
                '--password', svn_password],
                combine=True)

            return jsonify(message=result)
        else:
            return jsonify(message='File not allowed')

    @staticmethod
    def pack_fn(filepath, filepath_zip, paths_remap_relbase, all_deps, report, mode):
        """
        'paths_remap_relbase' is the project path,
        we want all paths to be relative to this so we don't get server path included.
        """
        import os
        from bam.blend import blendfile_pack
        assert(os.path.exists(filepath) and not os.path.isdir(filepath))
        log.info("  Source path: %r" % filepath)
        log.info("  Zip path: %r" % filepath_zip)

        deps_remap = {}
        paths_remap = {}
        paths_uuid = {}

        binary_edits = {}

        if filepath.endswith(".blend"):

            # find the path relative to the project's root
            blendfile_src_dir_fakeroot = os.path.dirname(os.path.relpath(filepath, paths_remap_relbase))

            try:
                yield from blendfile_pack.pack(
                        filepath.encode('utf-8'), filepath_zip.encode('utf-8'), mode=mode,
                        paths_remap_relbase=paths_remap_relbase.encode('utf-8'),
                        deps_remap=deps_remap, paths_remap=paths_remap, paths_uuid=paths_uuid,
                        all_deps=all_deps,
                        report=report,
                        blendfile_src_dir_fakeroot=blendfile_src_dir_fakeroot.encode('utf-8'),
                        readonly=True,
                        binary_edits=binary_edits,
                        )
            except:
                log.exception("Error packing the blend file")
                return
        else:
            # non blend-file
            from bam.utils.system import uuid_from_file
            paths_uuid[os.path.basename(filepath)] = uuid_from_file(filepath)
            del uuid_from_file

            import zipfile
            with zipfile.ZipFile(filepath_zip, 'w', zipfile.ZIP_DEFLATED) as zip_handle:
                zip_handle.write(
                        filepath,
                        arcname=os.path.basename(filepath),
                        )
            del zipfile

            # simple case
            paths_remap[os.path.basename(filepath)] = os.path.basename(filepath)

        if os.path.isfile(filepath):
            paths_remap["."] = os.path.relpath(os.path.dirname(filepath), paths_remap_relbase)
        else:
            # TODO(cam) directory support
            paths_remap["."] = os.path.relpath(filepath, paths_remap_relbase)

        # TODO, avoid reopening zipfile
        # append json info to zip
        import zipfile
        with zipfile.ZipFile(filepath_zip, 'a', zipfile.ZIP_DEFLATED) as zip_handle:
            import json

            def write_dict_as_json(f, dct):
                zip_handle.writestr(
                        f,
                        json.dumps(dct,
                        check_circular=False,
                        # optional (pretty)
                        sort_keys=True, indent=4, separators=(',', ': '),
                        ).encode('utf-8'))

            write_dict_as_json(".bam_deps_remap.json", deps_remap)
            write_dict_as_json(".bam_paths_remap.json", paths_remap)
            write_dict_as_json(".bam_paths_uuid.json", paths_uuid)

            import pickle
            zip_handle.writestr(".bam_paths_edit.data", pickle.dumps(binary_edits, pickle.HIGHEST_PROTOCOL))
            del write_dict_as_json

        del binary_edits
        # done writing json!

    @staticmethod
    def allowed_file(filename):
        return '.' in filename and \
            filename.rsplit('.', 1)[1] in app.config['ALLOWED_EXTENSIONS']
