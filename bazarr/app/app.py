# coding=utf-8

from flask import Flask, redirect
from flask_socketio import SocketIO

from .get_args import args
from .config import settings, base_url

socketio = SocketIO()


def create_app():
    # Flask Setup
    app = Flask(__name__)
    app.wsgi_app = ReverseProxied(app.wsgi_app)

    app.config["SECRET_KEY"] = settings.general.flask_secret_key
    app.config['JSONIFY_PRETTYPRINT_REGULAR'] = True
    app.config['JSON_AS_ASCII'] = False

    if args.dev:
        app.config["DEBUG"] = True
    else:
        app.config["DEBUG"] = False

    socketio.init_app(app, path=base_url.rstrip('/')+'/api/socket.io', cors_allowed_origins='*',
                      async_mode='threading', allow_upgrades=False, transports='polling')

    @app.errorhandler(404)
    def page_not_found(_):
        return redirect(base_url, code=302)

    return app


class ReverseProxied(object):
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        scheme = environ.get('HTTP_X_FORWARDED_PROTO')
        if scheme:
            environ['wsgi.url_scheme'] = scheme
        return self.app(environ, start_response)