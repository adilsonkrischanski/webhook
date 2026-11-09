import base64
import re

import pytest
from flask_migrate import upgrade
from sqlalchemy import inspect
from werkzeug.security import check_password_hash

from app import Endpoint, LoginAttempt, WebhookRequest, create_app, db


@pytest.fixture()
def app(tmp_path):
    app = create_app({'TESTING': True, 'SECRET_KEY': 'a' * 64, 'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD': 'test-password', 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path}/test.db', 'SESSION_COOKIE_SECURE': False, 'MAX_CONTENT_LENGTH': 16384})
    with app.app_context():
        upgrade()
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client):
    with client.session_transaction() as session:
        session.setdefault('csrf', 'csrf-test')
        return session['csrf']


def login(client):
    return client.post('/login', data={'csrf_token': csrf(client), 'username': 'admin', 'password': 'test-password'})


def post(client, url, **data):
    return client.post(url, data={'csrf_token': csrf(client), **data})


def create_endpoint(client, auth='none', path='/teste'):
    response = post(client, '/admin/endpoints/new', name='Teste', path=path, auth_type=auth, active='on')
    assert response.status_code == 200
    return response


def test_login_csrf_session_and_throttle(client, app):
    assert client.get('/admin').status_code == 302
    assert client.post('/login', data={'username': 'admin', 'password': 'test-password'}).status_code == 400
    for _ in range(10):
        assert post(client, '/login', username='admin', password='bad').status_code == 401
    assert login(client).status_code == 429
    with app.app_context():
        db.session.execute(db.delete(LoginAttempt))
        db.session.commit()
    assert login(client).status_code == 302
    assert client.get('/admin').status_code == 200
    assert post(client, '/logout').status_code == 302
    assert client.get('/admin').status_code == 302


@pytest.mark.parametrize('auth', ['none', 'bearer', 'basic', 'query'])
def test_auth_record_detail_and_rotation(client, app, auth):
    login(client)
    response = create_endpoint(client, auth)
    html = response.get_data(as_text=True)
    headers = {'Content-Type': 'application/json'}
    secret = None
    with app.app_context():
        endpoint = db.session.scalar(db.select(Endpoint))
        endpoint_id, username = endpoint.id, endpoint.username
        if auth != 'none':
            secret = re.findall(r'<input readonly value="([^"]+)"', html)[-1]
            assert check_password_hash(endpoint.secret_hash, secret)
            assert endpoint.secret_hash != secret
    if auth == 'bearer':
        headers['Authorization'] = 'Bearer ' + secret
    elif auth == 'basic':
        headers['Authorization'] = 'Basic ' + base64.b64encode(f'{username}:{secret}'.encode()).decode()
    token = secret if auth == 'query' else 'hidden'
    if auth == 'query':
        assert re.fullmatch(r'[0-9a-f]{64}', secret)
        assert f'/teste?token={secret}' in html
        unauthorized = client.post('/teste?token=wrong')
        assert unauthorized.status_code == 401
        assert 'WWW-Authenticate' not in unauthorized.headers
    if auth != 'none':
        assert client.post('/teste').status_code == 401
        assert client.post('/teste', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    response = client.post(f'/teste?token={token}&foo=bar', data=b'{"evento":"<script>alert(1)</script>"}', headers=headers)
    assert response.status_code == 200
    request_id = response.json['request_id']
    detail = client.get('/admin/requests/' + request_id)
    assert detail.status_code == 200
    assert '&lt;script&gt;' in detail.text
    assert '<script>alert' not in detail.text
    assert client.get('/admin/requests/' + request_id + '/download').data == b'{"evento":"<script>alert(1)</script>"}'
    with app.app_context():
        item = db.session.get(WebhookRequest, request_id)
        assert token not in item.query
        assert 'foo=bar' in item.query
        if auth in {'bearer', 'basic'}:
            assert secret not in item.headers
            assert '[REDACTED]' in item.headers
    assert client.get(f'/admin/endpoints/{endpoint_id}/history?auth=ok&method=POST').status_code == 200
    assert post(client, f'/admin/endpoints/{endpoint_id}/rotate').status_code == 200
    if auth != 'none':
        assert client.post(f'/teste?token={token}', headers=headers).status_code == 401
    assert client.get('/admin').status_code == 200


def test_replay_data_filters_sensitive_values(client, app):
    login(client)
    create_endpoint(client)
    body = b'\x00\xff{"peso":10}'
    request_id = client.post('/teste?token=secret&foo=bar', data=body, headers={'Authorization': 'Bearer x', 'X-Custom': 'keep', 'Cookie': 'a=b', 'Content-Type': 'application/json'}).json['request_id']
    data = client.get(f'/admin/requests/{request_id}/replay-data').json
    assert base64.b64decode(data['body_b64']) == body
    assert data['method'] == 'POST'
    assert data['query'] == [['foo', 'bar']]
    assert data['content_type'] == 'application/json'
    lowered = {k.lower(): v for k, v in data['headers'].items()}
    assert lowered['x-custom'] == 'keep'
    for name in ['authorization', 'cookie', 'host', 'content-length']:
        assert name not in lowered
    detail = client.get('/admin/requests/' + request_id)
    assert 'id="replay"' in detail.text
    assert "connect-src 'self' http://localhost:*" in detail.headers['Content-Security-Policy']
    post(client, '/logout')
    assert client.get(f'/admin/requests/{request_id}/replay-data').status_code == 302


def test_endpoint_validation_and_edit(client, app):
    login(client)
    for path in ['/admin/evil', '/login', '/', '/static/x', '/bad path', '/trailing/']:
        response = post(client, '/admin/endpoints/new', name='Bad', path=path, auth_type='none', active='on')
        assert response.status_code == 200
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Endpoint)) == 0
    create_endpoint(client)
    create_endpoint(client)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Endpoint)) == 1
        endpoint_id = db.session.scalar(db.select(Endpoint.id))
    assert post(client, f'/admin/endpoints/{endpoint_id}/edit', name='Changed', path='/novo', auth_type='basic', active='on').status_code == 200
    assert client.post('/teste').status_code == 404
    assert client.post('/novo').status_code == 401
    post(client, f'/admin/endpoints/{endpoint_id}/edit', name='Changed', path='/novo', auth_type='basic')
    assert client.post('/novo').status_code == 404


def test_binary_limit_methods_and_deletion(client, app):
    login(client)
    create_endpoint(client)
    with app.app_context():
        endpoint_id = db.session.scalar(db.select(Endpoint.id))
    for method in ['GET', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD']:
        assert client.open('/teste', method=method).status_code == 200
    response = client.post('/teste', data=b'\x00\xffbinary', content_type='application/octet-stream')
    request_id = response.json['request_id']
    assert 'Conteúdo binário' in client.get('/admin/requests/' + request_id).text
    assert client.post('/teste', data=b'x' * 17000).status_code == 413
    with app.app_context():
        assert db.session.scalar(db.select(WebhookRequest).where(WebhookRequest.status == 413)).body == b''
    post(client, f'/admin/endpoints/{endpoint_id}/history/delete', ids=request_id, scope='selected')
    assert client.get('/admin/requests/' + request_id).status_code == 404
    post(client, f'/admin/endpoints/{endpoint_id}/delete')
    assert client.post('/teste').status_code == 404
    assert client.get(f'/admin/endpoints/{endpoint_id}/history').status_code == 200
    post(client, f'/admin/endpoints/{endpoint_id}/history/delete', scope='all')
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(WebhookRequest)) == 0
    post(client, f'/admin/endpoints/{endpoint_id}/delete', delete_history='on')
    assert client.get(f'/admin/endpoints/{endpoint_id}/history').status_code == 404


def test_admin_isolation_and_headers(client, app):
    login(client)
    create_endpoint(client)
    item = client.post('/teste').json['request_id']
    assert client.get('/admin/endpoints/1/history?start=bad').status_code == 400
    assert client.get('/admin/endpoints/1/history?start=2020-01-01&end=2099-01-01').status_code == 200
    assert client.get('/admin/endpoints/1/history?page=500').status_code == 404
    response = client.get('/admin/requests/' + item)
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    post(client, '/logout')
    for path in ['/admin', '/admin/requests/' + item, '/admin/requests/' + item + '/download', '/admin/endpoints/1/edit', '/admin/endpoints/1/history']:
        assert client.get(path).status_code == 302
    assert client.post('/admin/endpoints/1/rotate').status_code == 400
    assert client.post('/not-found').status_code == 404
    assert client.get('/healthz').json == {'status': 'ok'}
    with app.app_context():
        assert 'alembic_version' in inspect(db.engine).get_table_names()


def test_password_change_invalidates_session(client, app):
    login(client)
    app.config['ADMIN_PASSWORD'] = 'different-password'
    assert client.get('/admin').status_code == 302


def test_pagination_and_delete_isolation(client, app):
    login(client)
    create_endpoint(client, path='/one')
    create_endpoint(client, path='/two')
    one_id = client.post('/one').json['request_id']
    two_id = client.post('/two').json['request_id']
    with app.app_context():
        one = db.session.scalar(db.select(Endpoint).where(Endpoint.path == '/one'))
        endpoint_id = one.id
        for _ in range(30):
            db.session.add(WebhookRequest(endpoint_id=one.id, method='POST', path='/one', query='', headers='{}', body=b'', authenticated=True, status=200))
        db.session.commit()
    response = client.get(f'/admin/endpoints/{endpoint_id}/history')
    assert response.status_code == 200
    assert 'Página 1 de 2' in response.text
    assert client.get(f'/admin/endpoints/{endpoint_id}/history?page=2').status_code == 200
    post(client, f'/admin/endpoints/{endpoint_id}/history/delete', ids=two_id, scope='selected')
    assert client.get('/admin/requests/' + two_id).status_code == 200
    post(client, f'/admin/endpoints/{endpoint_id}/history/delete', scope='all')
    assert client.get('/admin/requests/' + one_id).status_code == 404
    assert client.get('/admin/requests/' + two_id).status_code == 200
