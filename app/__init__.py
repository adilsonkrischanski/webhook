import hashlib
import hmac
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qsl, urlencode

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import RequestEntityTooLarge

db = SQLAlchemy()


@event.listens_for(Engine, 'connect')
def sqlite_settings(connection, record):
    if connection.__class__.__module__.startswith('sqlite3'):
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=30000')
        connection.execute('PRAGMA journal_mode=WAL')


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Endpoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    path = db.Column(db.String(240), unique=True, nullable=False)
    auth_type = db.Column(db.String(10), nullable=False)
    username = db.Column(db.String(100))
    secret_hash = db.Column(db.String(256))
    active = db.Column(db.Boolean, default=True, nullable=False)
    deleted = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=now, nullable=False)


class WebhookRequest(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=lambda: secrets.token_hex(16))
    endpoint_id = db.Column(db.Integer, db.ForeignKey('endpoint.id'), nullable=False, index=True)
    endpoint = db.relationship(Endpoint)
    created_at = db.Column(db.DateTime, default=now, index=True, nullable=False)
    method = db.Column(db.String(10), nullable=False)
    path = db.Column(db.Text, nullable=False)
    query = db.Column(db.Text, nullable=False)
    headers = db.Column(db.Text, nullable=False)
    body = db.Column(db.LargeBinary, nullable=False)
    content_type = db.Column(db.Text)
    ip = db.Column(db.String(100))
    authenticated = db.Column(db.Boolean, nullable=False)
    status = db.Column(db.Integer, nullable=False)


class LoginAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(100), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now, nullable=False, index=True)


SENSITIVE = {'authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'x-api-key', 'api-key', 'token', 'access_token', 'password', 'secret', 'api_key'}


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv('SECRET_KEY'),
        ADMIN_USERNAME=os.getenv('ADMIN_USERNAME'),
        ADMIN_PASSWORD=os.getenv('ADMIN_PASSWORD'),
        SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL', 'sqlite:////data/webhooks.db'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=int(os.getenv('MAX_BODY_BYTES', '1048576')),
        PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL', 'https://webhook.krischanski.dev').rstrip('/'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', 'true').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_FORM_MEMORY_SIZE=256000,
    )
    if config:
        app.config.update(config)
    for key in ('SECRET_KEY', 'ADMIN_USERNAME', 'ADMIN_PASSWORD'):
        if not app.config.get(key):
            raise RuntimeError(f'Configure {key} no ambiente.')
    if len(app.config['SECRET_KEY']) < 32:
        raise RuntimeError('SECRET_KEY precisa ter pelo menos 32 caracteres.')
    if os.getenv('TRUST_PROXY', 'false').lower() == 'true':
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    db.init_app(app)
    Migrate(app, db)

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    app.jinja_env.globals['csrf_token'] = csrf
    app.jinja_env.globals['public_url'] = lambda endpoint: app.config['PUBLIC_BASE_URL'] + endpoint.path

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            fingerprint = hmac.new(app.config['SECRET_KEY'].encode(), (app.config['ADMIN_USERNAME'] + '\0' + app.config['ADMIN_PASSWORD']).encode(), hashlib.sha256).hexdigest()
            if not session.get('admin') or not hmac.compare_digest(session.get('fingerprint', ''), fingerprint):
                session.clear()
                return redirect(url_for('login'))
            return view(*args, **kwargs)
        return wrapped

    @app.before_request
    def protect_forms():
        # Only administrative views use session authentication. Webhooks never require CSRF.
        if request.endpoint in {'login', 'logout', 'endpoint_new', 'endpoint_edit', 'rotate', 'endpoint_delete', 'history_delete'} and request.method == 'POST':
            if not hmac.compare_digest(session.get('csrf', ''), request.form.get('csrf_token', '')) or not session.get('csrf'):
                abort(400, 'Formulário expirado. Recarregue a página.')

    @app.after_request
    def secure_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def health():
        db.session.execute(db.text('SELECT 1'))
        return jsonify(status='ok')

    @app.get('/')
    def index():
        return redirect(url_for('dashboard'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            ip = request.remote_addr or 'unknown'
            cutoff = now() - timedelta(minutes=15)
            db.session.execute(db.delete(LoginAttempt).where(LoginAttempt.created_at < cutoff))
            count = db.session.scalar(db.select(db.func.count()).select_from(LoginAttempt).where(LoginAttempt.ip == ip))
            if count >= 10:
                db.session.commit()
                return render_template('login.html', error='Muitas tentativas. Aguarde 15 minutos.'), 429
            valid_user = hmac.compare_digest(request.form.get('username', '').encode(), app.config['ADMIN_USERNAME'].encode())
            valid_pass = hmac.compare_digest(request.form.get('password', '').encode(), app.config['ADMIN_PASSWORD'].encode())
            if valid_user and valid_pass:
                db.session.execute(db.delete(LoginAttempt).where(LoginAttempt.ip == ip))
                db.session.commit()
                session.clear()
                session['admin'] = True
                session['fingerprint'] = hmac.new(app.config['SECRET_KEY'].encode(), (app.config['ADMIN_USERNAME'] + '\0' + app.config['ADMIN_PASSWORD']).encode(), hashlib.sha256).hexdigest()
                session.permanent = True
                return redirect(url_for('dashboard'))
            db.session.add(LoginAttempt(ip=ip))
            db.session.commit()
            return render_template('login.html', error='Usuário ou senha inválidos.'), 401
        return render_template('login.html')

    @app.post('/logout')
    @login_required
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.get('/admin')
    @login_required
    def dashboard():
        endpoints = db.session.scalars(db.select(Endpoint).order_by(Endpoint.deleted, Endpoint.created_at.desc())).all()
        counts = dict(db.session.execute(db.select(WebhookRequest.endpoint_id, db.func.count()).group_by(WebhookRequest.endpoint_id)).all())
        return render_template('dashboard.html', endpoints=endpoints, counts=counts)

    def get_endpoint(endpoint_id):
        return db.get_or_404(Endpoint, endpoint_id)

    def generate_credentials(endpoint):
        secret = secrets.token_urlsafe(32)
        endpoint.username = 'wh_' + secrets.token_hex(6) if endpoint.auth_type == 'basic' else None
        endpoint.secret_hash = generate_password_hash(secret) if endpoint.auth_type != 'none' else None
        return secret if endpoint.auth_type != 'none' else None

    def save_endpoint(endpoint):
        name = request.form.get('name', '').strip()
        path = request.form.get('path', '').strip()
        auth_type = request.form.get('auth_type')
        if not name or len(name) > 120:
            raise ValueError('Informe um nome com até 120 caracteres.')
        if not re.fullmatch(r'/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', path) or len(path) > 240:
            raise ValueError('Use um caminho como /teste ou /integracoes/pedidos, sem barra final.')
        if path.split('/')[1] in {'admin', 'login', 'logout', 'static', 'healthz'}:
            raise ValueError('Esse caminho está reservado pela aplicação.')
        duplicate = db.session.scalar(db.select(Endpoint).where(Endpoint.path == path, Endpoint.id != (endpoint.id or 0)))
        if duplicate:
            raise ValueError('Esse caminho já está cadastrado, inclusive no arquivo de endpoints excluídos.')
        if auth_type not in {'none', 'bearer', 'basic'}:
            raise ValueError('Selecione uma autenticação válida.')
        changed = endpoint.auth_type != auth_type
        endpoint.name, endpoint.path, endpoint.auth_type = name, path, auth_type
        endpoint.active = request.form.get('active') == 'on'
        secret = generate_credentials(endpoint) if changed else None
        db.session.add(endpoint)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise ValueError('Esse caminho já foi cadastrado por outra operação.')
        return secret, changed

    @app.route('/admin/endpoints/new', methods=['GET', 'POST'])
    @login_required
    def endpoint_new():
        endpoint = Endpoint(active=True)
        if request.method == 'POST':
            try:
                secret, _ = save_endpoint(endpoint)
                return render_template('credentials.html', endpoint=endpoint, secret=secret)
            except ValueError as exc:
                flash(str(exc), 'error')
        return render_template('endpoint_form.html', endpoint=endpoint)

    @app.route('/admin/endpoints/<int:endpoint_id>/edit', methods=['GET', 'POST'])
    @login_required
    def endpoint_edit(endpoint_id):
        endpoint = get_endpoint(endpoint_id)
        if endpoint.deleted:
            abort(404)
        if request.method == 'POST':
            try:
                secret, changed = save_endpoint(endpoint)
                if changed:
                    return render_template('credentials.html', endpoint=endpoint, secret=secret)
                flash('Endpoint atualizado.', 'success')
                return redirect(url_for('dashboard'))
            except ValueError as exc:
                flash(str(exc), 'error')
        return render_template('endpoint_form.html', endpoint=endpoint)

    @app.post('/admin/endpoints/<int:endpoint_id>/rotate')
    @login_required
    def rotate(endpoint_id):
        endpoint = get_endpoint(endpoint_id)
        if endpoint.deleted:
            abort(404)
        secret = generate_credentials(endpoint)
        db.session.commit()
        return render_template('credentials.html', endpoint=endpoint, secret=secret)

    @app.post('/admin/endpoints/<int:endpoint_id>/delete')
    @login_required
    def endpoint_delete(endpoint_id):
        endpoint = get_endpoint(endpoint_id)
        if request.form.get('delete_history') == 'on':
            db.session.execute(db.delete(WebhookRequest).where(WebhookRequest.endpoint_id == endpoint.id))
            db.session.delete(endpoint)
        else:
            endpoint.deleted, endpoint.active, endpoint.secret_hash = True, False, None
        db.session.commit()
        flash('Endpoint excluído.', 'success')
        return redirect(url_for('dashboard'))

    def history_query(endpoint_id):
        query = db.select(WebhookRequest).where(WebhookRequest.endpoint_id == endpoint_id)
        method = request.args.get('method', '')
        if method:
            query = query.where(WebhookRequest.method == method)
        auth = request.args.get('auth', '')
        if auth in {'ok', 'failed'}:
            query = query.where(WebhookRequest.authenticated == (auth == 'ok'))
        for param, upper in [('start', False), ('end', True)]:
            value = request.args.get(param)
            if value:
                try:
                    date = datetime.strptime(value, '%Y-%m-%d')
                except ValueError:
                    abort(400, 'Data inválida.')
                query = query.where(WebhookRequest.created_at < date + timedelta(days=1)) if upper else query.where(WebhookRequest.created_at >= date)
        return query

    @app.get('/admin/endpoints/<int:endpoint_id>/history')
    @login_required
    def history(endpoint_id):
        endpoint = get_endpoint(endpoint_id)
        page = db.paginate(history_query(endpoint_id).order_by(WebhookRequest.created_at.desc()), per_page=30, max_per_page=30)
        filters = {k: request.args.get(k, '') for k in ('method', 'auth', 'start', 'end')}
        return render_template('history.html', endpoint=endpoint, page=page, filters=filters)

    @app.post('/admin/endpoints/<int:endpoint_id>/history/delete')
    @login_required
    def history_delete(endpoint_id):
        get_endpoint(endpoint_id)
        query = db.delete(WebhookRequest).where(WebhookRequest.endpoint_id == endpoint_id)
        if request.form.get('scope') != 'all':
            query = query.where(WebhookRequest.id.in_(request.form.getlist('ids')))
        db.session.execute(query)
        db.session.commit()
        flash('Registros apagados.', 'success')
        return redirect(url_for('history', endpoint_id=endpoint_id))

    @app.get('/admin/requests/<request_id>')
    @login_required
    def detail(request_id):
        item = db.get_or_404(WebhookRequest, request_id)
        try:
            body = item.body.decode('utf-8')
            if any(ord(char) < 32 and char not in '\n\r\t' for char in body):
                body = None
            if body is not None:
                try:
                    body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
                except ValueError:
                    pass
        except UnicodeDecodeError:
            body = None
        return render_template('detail.html', item=item, body=body, headers=json.dumps(json.loads(item.headers), ensure_ascii=False, indent=2))

    @app.get('/admin/requests/<request_id>/download')
    @login_required
    def download(request_id):
        item = db.get_or_404(WebhookRequest, request_id)
        return send_file(io.BytesIO(item.body), mimetype='application/octet-stream', as_attachment=True, download_name=f'{item.id}.bin')

    @app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'], provide_automatic_options=False)
    def receive(path):
        if path.split('/')[0] in {'admin', 'login', 'logout', 'static', 'healthz'}:
            abort(404)
        endpoint = db.session.scalar(db.select(Endpoint).where(Endpoint.path == '/' + path, Endpoint.active.is_(True), Endpoint.deleted.is_(False)))
        if not endpoint:
            abort(404)
        valid = endpoint.auth_type == 'none'
        auth = request.authorization
        if endpoint.auth_type == 'bearer' and auth and auth.type.lower() == 'bearer':
            valid = check_password_hash(endpoint.secret_hash, auth.token or '')
        elif endpoint.auth_type == 'basic' and auth and auth.type.lower() == 'basic':
            valid = hmac.compare_digest((auth.username or '').encode(), endpoint.username.encode()) and check_password_hash(endpoint.secret_hash, auth.password or '')
        status = 200 if valid else 401
        try:
            body = request.get_data()
        except RequestEntityTooLarge:
            body, status = b'', 413
        headers = {key: '[REDACTED]' if key.lower() in SENSITIVE else value for key, value in request.headers.items()}
        query = urlencode([(k, '[REDACTED]' if k.lower() in SENSITIVE else v) for k, v in parse_qsl(request.query_string.decode('utf-8', errors='replace'), keep_blank_values=True)])
        item = WebhookRequest(endpoint_id=endpoint.id, method=request.method, path=request.path, query=query, headers=json.dumps(headers), body=body, content_type=request.content_type, ip=request.remote_addr, authenticated=valid, status=status)
        db.session.add(item)
        db.session.commit()
        response = jsonify(status={200: 'received', 401: 'unauthorized', 413: 'payload_too_large'}[status], request_id=item.id)
        response.status_code = status
        if status == 401:
            response.headers['WWW-Authenticate'] = 'Basic realm="webhook"' if endpoint.auth_type == 'basic' else 'Bearer'
        return response

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error(exc):
        return render_template('error.html', error=exc), exc.code

    return app
