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
Environment vars:
- BAM_VERBOSE, set to get debug logging.
"""


# ------------------
# Ensure module path
import os
import sys
path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "modules"))
if path not in sys.path:
    sys.path.append(path)
del os, sys, path
# --------

import os
import json
import svn.local
import werkzeug
import xml.etree.ElementTree
import logging

from flask import Flask, jsonify, abort, request, make_response, url_for, Response
from flask.views import MethodView
from flask.ext.restful import Api, Resource, reqparse, fields, marshal
from flask.ext.httpauth import HTTPBasicAuth
from flask.ext.sqlalchemy import SQLAlchemy

app = Flask(__name__)
api = Api(app)
auth = HTTPBasicAuth()

try:
    import config
except ImportError:
    config = None

if config is None:
    app.config["ALLOWED_EXTENSIONS"] = {'txt', 'mp4', 'png', 'jpg', 'jpeg', 'gif', 'blend', 'zip'}
else:
    app.config.from_object(config.Development)

db = SQLAlchemy(app)

from application.modules.admin import backend
from application.modules.admin import settings
from application.modules.projects import admin
from application.modules.projects.model import Project, ProjectSetting

log = logging.getLogger("webservice")

if os.environ.get("BAM_VERBOSE"):
    logging.basicConfig(level=logging.DEBUG)


@auth.get_password
def get_password(username):
    # Temporarily override API access
    # TODO (fsiddi) check against users table
    return ''
    if username == 'bam':
        return 'bam'
    return None


@auth.error_handler
def unauthorized():
    return make_response(jsonify({'message': 'Unauthorized access'}), 403)
    # return 403 instead of 401 to prevent browsers from displaying
    # the default auth dialog


class DirectoryAPI(Resource):
    """Displays list of files."""

    decorators = [auth.login_required]

    def __init__(self):
        parser = reqparse.RequestParser()
        #parser.add_argument('rate', type=int, help='Rate cannot be converted')
        parser.add_argument('path', type=str)
        args = parser.parse_args()
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
        args = parser.parse_args()

        super(FileAPI, self).__init__()

    def get(self, project_name):
        filepath = request.args['filepath']
        command = request.args['command']

        project = Project.query.filter_by(name=project_name).first()

        if command == 'info':
            r = svn.local.LocalClient(project.repository_path)

            log = r.log_default(None, None, 5, filepath)
            log = [l for l in log]

            return jsonify(
                filepath=filepath,
                log=log)

        elif command == 'checkout':
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

                yield from self.pack_fn(filepath, filepath_zip, project.repository_path, report)

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

        else:
            return jsonify(message="Command unknown")

    def put(self, project_name):
        project = Project.query.filter_by(name=project_name).first()
        command = request.args['command']
        arguments = ''
        if 'arguments' in request.args:
            arguments = json.loads(request.args['arguments'])
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
                '--message', arguments['message'],
                '--username', svn_user,
                '--password', svn_password],
                combine=True)

            return jsonify(message=result)
        else:
            return jsonify(message='File not allowed')

    @staticmethod
    def pack_fn(filepath, filepath_zip, paths_remap_relbase, report):
        """
        'paths_remap_relbase' is the project path,
        we want all paths to be relative to this so we don't get server path included.
        """
        import os
        import blendfile_pack

        assert(os.path.exists(filepath) and not os.path.isdir(filepath))
        log.info("  Source path: %r" % filepath)
        log.info("  Zip path: %r" % filepath_zip)

        deps_remap = {}
        paths_remap = {}
        paths_uuid = {}

        if filepath.endswith(".blend"):
            try:
                yield from blendfile_pack.pack(
                        filepath.encode('utf-8'), filepath_zip.encode('utf-8'), mode='ZIP',
                        paths_remap_relbase=paths_remap_relbase.encode('utf-8'),
                        # TODO(cam) this just means the json is written in the zip
                        deps_remap=deps_remap, paths_remap=paths_remap, paths_uuid=paths_uuid,
                        report=report)
            except:
                log.exception("Error packing the blend file")
                return
        else:
            # non blend-file
            from bam_utils.system import sha1_from_file
            paths_uuid[os.path.basename(filepath)] = sha1_from_file(filepath)
            del sha1_from_file

            import zipfile
            with zipfile.ZipFile(filepath_zip, 'w', zipfile.ZIP_DEFLATED) as zip_handle:
                zip_handle.write(
                        filepath,
                        arcname=os.path.basename(filepath),
                        )
            del zipfile

            # simple case
            paths_remap[os.path.basename(filepath)] = os.path.basename(filepath)

        # TODO, avoid reopening zipfile
        # append json info to zip
        import zipfile
        with zipfile.ZipFile(filepath_zip, 'a', zipfile.ZIP_DEFLATED) as zip_handle:
            import json

            def write_dict_as_json(fn, dct):
                zip_handle.writestr(
                        fn,
                        json.dumps(dct,
                        check_circular=False,
                        # optional (pretty)
                        sort_keys=True, indent=4, separators=(',', ': '),
                        ).encode('utf-8'))

            write_dict_as_json(".bam_deps_remap.json", deps_remap)
            write_dict_as_json(".bam_paths_remap.json", paths_remap)
            write_dict_as_json(".bam_paths_uuid.json", paths_uuid)

            del write_dict_as_json
        # done writing json!

    @staticmethod
    def allowed_file(filename):
        return '.' in filename and \
            filename.rsplit('.', 1)[1] in app.config['ALLOWED_EXTENSIONS']


api.add_resource(DirectoryAPI, '/<project_name>/file_list', endpoint='file_list')
api.add_resource(FileAPI, '/<project_name>/file', endpoint='file')
