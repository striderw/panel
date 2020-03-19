"""
Subclasses the bokeh serve commandline handler to extend it in various
ways.
"""

import argparse
import logging # isort:skip
import os

from fnmatch import fnmatch
from glob import glob
from typing import List

from bokeh.application import Application
from bokeh.command.subcommands.serve import Serve as _BkServe
from bokeh.command.util import build_single_handler_applications, die, report_server_init_errors
from bokeh.server.auth_provider import AuthModule, NullAuth
from bokeh.settings import settings
from bokeh.util.logconfig import basicConfig
from tornado.autoreload import watch
from tornado.wsgi import WSGIContainer
from tornado.web import FallbackHandler

from ..io.server import INDEX_HTML

log = logging.getLogger(__name__)


class Serve(_BkServe):

    args = _BkServe.args + (
        ('--tranquilize', dict(
            action = 'store_true',
            help   = "Discover and serve tranquilized functions",
        )),
    )

    def invoke(self, args: argparse.Namespace) -> None:
        '''

        '''
        basicConfig(format=args.log_format, filename=args.log_file)

        # This is a bit of a fudge. We want the default log level for non-server
        # cases to be None, i.e. we don't set a log level. But for the server we
        # do want to set the log level to INFO if nothing else overrides that.
        log_level = settings.py_log_level(args.log_level)
        if log_level is None:
            log_level = logging.INFO
        logging.getLogger('bokeh').setLevel(log_level)

        if args.use_config is not None:
            log.info("Using override config file: {}".format(args.use_config))
            settings.load_config(args.use_config)

        # protect this import inside a function so that "bokeh info" can work
        # even if Tornado is not installed
        from bokeh.server.server import Server

        files = []
        for f in args.files:
            if args.glob:
                files.extend(glob(f))
            else:
                files.append(f)

        argvs = { f : args.args for f in files}
        applications = build_single_handler_applications(files, argvs)

        if len(applications) == 0:
            # create an empty application by default
            applications['/'] = Application()

        # rename args to be compatible with Server
        if args.keep_alive is not None:
            args.keep_alive_milliseconds = args.keep_alive

        if args.check_unused_sessions is not None:
            args.check_unused_sessions_milliseconds = args.check_unused_sessions

        if args.unused_session_lifetime is not None:
            args.unused_session_lifetime_milliseconds = args.unused_session_lifetime

        if args.stats_log_frequency is not None:
            args.stats_log_frequency_milliseconds = args.stats_log_frequency

        if args.mem_log_frequency is not None:
            args.mem_log_frequency_milliseconds = args.mem_log_frequency

        server_kwargs = { key: getattr(args, key) for key in ['port',
                                                              'address',
                                                              'allow_websocket_origin',
                                                              'num_procs',
                                                              'prefix',
                                                              'index',
                                                              'keep_alive_milliseconds',
                                                              'check_unused_sessions_milliseconds',
                                                              'unused_session_lifetime_milliseconds',
                                                              'stats_log_frequency_milliseconds',
                                                              'mem_log_frequency_milliseconds',
                                                              'use_xheaders',
                                                              'websocket_max_message_size',
                                                              'include_cookies',
                                                              'include_headers',
                                                              'exclude_cookies',
                                                              'exclude_headers',
                                                              'session_token_expiration',
                                                            ]
                          if getattr(args, key, None) is not None }

        if 'index' not in server_kwargs:
            server_kwargs['index'] = INDEX_HTML

        # Handle tranquilized functions in the supplied functions
        if args.tranquilize:
            from ..io.tranquilize import build_single_handler_application
            app = build_single_handler_application(files, args)
            tr = WSGIContainer(app)
            server_kwargs['extra_patterns'] = [(r".*", FallbackHandler, dict(fallback=tr))]

        server_kwargs['sign_sessions'] = settings.sign_sessions()
        server_kwargs['secret_key'] = settings.secret_key_bytes()
        server_kwargs['ssl_certfile'] = settings.ssl_certfile(getattr(args, 'ssl_certfile', None))
        server_kwargs['ssl_keyfile'] = settings.ssl_keyfile(getattr(args, 'ssl_keyfile', None))
        server_kwargs['ssl_password'] = settings.ssl_password()
        server_kwargs['generate_session_ids'] = True
        if args.session_ids is None:
            # no --session-ids means use the env vars
            pass
        elif args.session_ids == 'unsigned':
            server_kwargs['sign_sessions'] = False
        elif args.session_ids == 'signed':
            server_kwargs['sign_sessions'] = True
        elif args.session_ids == 'external-signed':
            server_kwargs['sign_sessions'] = True
            server_kwargs['generate_session_ids'] = False
        else:
            raise RuntimeError("argparse should have filtered out --session-ids mode " +
                               args.session_ids)

        if server_kwargs['sign_sessions'] and not server_kwargs['secret_key']:
            die("To sign sessions, the BOKEH_SECRET_KEY environment variable must be set; " +
                "the `bokeh secret` command can be used to generate a new key.")

        auth_module_path = settings.auth_module(getattr(args, 'auth_module', None))
        if auth_module_path:
            server_kwargs['auth_provider'] = AuthModule(auth_module_path)
        else:
            server_kwargs['auth_provider'] = NullAuth()

        server_kwargs['xsrf_cookies'] = settings.xsrf_cookies(getattr(args, 'enable_xsrf_cookies', False))
        server_kwargs['cookie_secret'] = settings.cookie_secret(getattr(args, 'cookie_secret', None))
        server_kwargs['use_index'] = not args.disable_index
        server_kwargs['redirect_root'] = not args.disable_index_redirect
        server_kwargs['autoreload'] = args.dev is not None

        def find_autoreload_targets(app_path: str) -> None:
            path = os.path.abspath(app_path)
            if not os.path.isdir(path):
                return

            for path, subdirs, files in os.walk(path):
                for name in files:
                    if (fnmatch(name, '*.html') or
                        fnmatch(name, '*.css') or
                        fnmatch(name, '*.yaml')):
                        log.info("Watching: " + os.path.join(path, name))
                        watch(os.path.join(path, name))

        def add_optional_autoreload_files(file_list: List[str]) -> None:
            for filen in file_list:
                if os.path.isdir(filen):
                    log.warning("Cannot watch directory " + filen)
                    continue
                log.info("Watching: " + filen)
                watch(filen)

        if server_kwargs['autoreload']:
            if len(applications.keys()) != 1:
                die("--dev can only support a single app.")
            if server_kwargs['num_procs'] != 1:
                log.info("Running in --dev mode. --num-procs is limited to 1.")
                server_kwargs['num_procs'] = 1

            find_autoreload_targets(args.files[0])
            add_optional_autoreload_files(args.dev)

        with report_server_init_errors(**server_kwargs):
            server = Server(applications, **server_kwargs)

            if args.show:

                # we have to defer opening in browser until we start up the server
                def show_callback() -> None:
                    for route in applications.keys():
                        server.show(route)

                server.io_loop.add_callback(show_callback)

            address_string = 'localhost'
            if server.address is not None and server.address != '':
                address_string = server.address

            for route in sorted(applications.keys()):
                url = "http://%s:%d%s%s" % (address_string, server.port, server.prefix, route)
                log.info("Panel app running at: %s" % url)

            log.info("Starting Panel server with process id: %d" % os.getpid())
            server.run_until_shutdown()
