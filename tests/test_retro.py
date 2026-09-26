"""Retro ROM library and retro requests: scanning, serving, save states, request flow."""
import os
import shutil
import unittest
import uuid
from unittest.mock import patch

_IMPORT_ERROR = None
try:
    from flask import Flask
    from app.app import app as main_app  # noqa: F401 - registers the user loader and models
    from app.auth import login_manager, auth_blueprint
    from app.db import db, User
    from app import roms as roms_mod
    from app import rom_requests as req_mod
except ModuleNotFoundError as exc:
    _IMPORT_ERROR = exc

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TMP_ROOT = os.path.join(REPO_DIR, '.tmp')


class RetroTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"Missing dependency for retro tests: {_IMPORT_ERROR}")

    def setUp(self):
        self.tmp = os.path.join(TMP_ROOT, f'retro-{uuid.uuid4().hex[:8]}')
        os.makedirs(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.roms_root = os.path.join(self.tmp, 'roms')
        for rel, data in {
            'gba/Example Title (USA).gba': b'x' * 1024,
            'gba/sub/Example Quest.gba': b'y' * 10,
            'gba/Example Quest.sav': b's',            # wrong extension, ignored
            'genesis/Example Racer.md': b'z',         # alias -> megadrive
            'unknown/thing.bin': b'q',                # unknown platform, ignored
        }.items():
            path = os.path.join(self.roms_root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(data)

        patches = [
            patch.object(roms_mod, 'ROMS_ROOT', self.roms_root),
            patch.object(roms_mod, 'STATES_DIR', os.path.join(self.tmp, 'states')),
            patch('app.auth.admin_account_created', return_value=True),
            patch.object(roms_mod, 'admin_account_created', return_value=True),
            patch.object(req_mod, 'admin_account_created', return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        notify_patch = patch.object(req_mod, 'notify', side_effect=self.sent.append)
        notify_patch.start()
        self.addCleanup(notify_patch.stop)

        app_dir = os.path.join(REPO_DIR, 'app')
        self.app = Flask('retro_test', template_folder=os.path.join(app_dir, 'templates'),
                         static_folder=os.path.join(app_dir, 'static'))
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(self.tmp, 'test.db'),
            SECRET_KEY='test', TESTING=True,
        )
        db.init_app(self.app)
        login_manager.init_app(self.app)
        self.app.register_blueprint(auth_blueprint)
        self.app.register_blueprint(roms_mod.roms_blueprint)
        self.app.register_blueprint(req_mod.requests_blueprint)
        with self.app.app_context():
            db.create_all()
            self.admin_id = self._make_user('exampleadmin', admin=True)
            self.user_a = self._make_user('examplea')
            self.user_b = self._make_user('exampleb')
            roms_mod.scan_roms()
        self.addCleanup(self._dispose)

    def _dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _make_user(self, name, admin=False):
        u = User(user=name, password='x', admin_access=admin, shop_access=True, backup_access=False)
        db.session.add(u)
        db.session.commit()
        return u.id

    def client_for(self, user_id):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s['_user_id'] = str(user_id)
            s['_fresh'] = True
        return c

    def rom_id(self, name):
        with self.app.app_context():
            return roms_mod.Rom.query.filter_by(name=name).one().id


class RetroLibraryTests(RetroTestBase):
    def test_scan_finds_platform_roms_and_removes_missing(self):
        with self.app.app_context():
            names = {(r.platform, r.name) for r in roms_mod.Rom.query.all()}
            self.assertEqual(names, {('gba', 'Example Title (USA)'), ('gba', 'Example Quest'),
                                     ('megadrive', 'Example Racer')})
            os.remove(os.path.join(self.roms_root, 'genesis', 'Example Racer.md'))
            self.assertEqual(roms_mod.scan_roms(), (0, 1, 2))

    def test_pages_and_file_download(self):
        c = self.client_for(self.user_a)
        rid = self.rom_id('Example Title (USA)')
        page = c.get('/retro')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Example Title (USA)', page.data)
        self.assertIn(b'Game Boy Advance', page.data)
        self.assertNotIn(b'id="rescanBtn"', page.data)  # not an admin
        play = c.get(f'/retro/play/{rid}')
        self.assertEqual(play.status_code, 200)
        self.assertIn(b'EJS_core = "gba"', play.data)
        f = c.get(f'/api/retro/roms/{rid}/file?download=1')
        self.assertEqual(f.status_code, 200)
        self.assertEqual(len(f.data), 1024)
        self.assertIn('attachment', f.headers['Content-Disposition'])
        f.close()
        self.assertEqual(c.get('/api/retro/roms/9999/file').status_code, 404)

    def test_anonymous_is_redirected(self):
        r = self.app.test_client().get('/retro')
        self.assertIn(r.status_code, (302, 401))

    def test_scan_is_admin_only(self):
        self.assertEqual(self.client_for(self.user_a).post('/api/retro/scan').status_code, 403)
        admin = self.client_for(self.admin_id)
        self.assertIn(b'id="rescanBtn"', admin.get('/retro').data)
        r = admin.post('/api/retro/scan')
        self.assertEqual(r.get_json()['total'], 3)

    def test_save_states_are_per_user(self):
        rid = self.rom_id('Example Quest')
        a, b = self.client_for(self.user_a), self.client_for(self.user_b)
        url = f'/api/retro/roms/{rid}/state'
        self.assertEqual(a.get(url).status_code, 404)
        self.assertEqual(a.put(url, data=b'\x01state-a').status_code, 200)
        self.assertEqual(a.get(url).data, b'\x01state-a')
        self.assertEqual(b.get(url).status_code, 404)
        self.assertIn(b'EJS_loadStateURL', a.get(f'/retro/play/{rid}').data)
        self.assertNotIn(b'EJS_loadStateURL', b.get(f'/retro/play/{rid}').data)
        self.assertEqual(a.delete(url).status_code, 200)
        self.assertEqual(a.get(url).status_code, 404)
        self.assertEqual(a.put(url, data=b'').status_code, 400)


class RetroRequestTests(RetroTestBase):
    def _create(self, client, title, platform='gba', note=None):
        return client.post('/api/retro/requests', json={'title': title, 'platform': platform, 'note': note})

    def test_normalize_title(self):
        self.assertEqual(req_mod.normalize_title('Example Title (USA, Europe) [!]'), 'example title')
        self.assertEqual(req_mod.normalize_title('Example & Friends: Part 2'), 'example and friends part 2')

    def test_validation_and_already_owned(self):
        a = self.client_for(self.user_a)
        self.assertEqual(self._create(a, '').status_code, 400)
        self.assertEqual(self._create(a, '(USA)').status_code, 400)
        self.assertEqual(self._create(a, 'Example', platform='nope').status_code, 400)
        owned = self._create(a, 'example title')
        self.assertEqual(owned.status_code, 409)
        self.assertEqual(owned.get_json()['rom_id'], self.rom_id('Example Title (USA)'))

    def test_second_user_joins_existing_request(self):
        a, b, admin = self.client_for(self.user_a), self.client_for(self.user_b), self.client_for(self.admin_id)
        first = self._create(a, 'Example Missing', note='EU version')
        self.assertEqual(first.status_code, 201)
        rid = first.get_json()['request']['id']

        again = self._create(a, 'Example Missing')
        self.assertFalse(again.get_json()['joined'])

        joined = self._create(b, 'example missing (europe)', note='any region')
        self.assertTrue(joined.get_json()['joined'])
        self.assertEqual(joined.get_json()['request']['id'], rid)
        self.assertEqual(joined.get_json()['request']['requester_count'], 2)

        mine_b = b.get('/api/retro/requests').get_json()['requests']
        self.assertEqual(len(mine_b), 1)
        self.assertTrue(mine_b[0]['is_mine'])
        self.assertEqual(mine_b[0]['my_note'], 'any region')
        self.assertNotIn('requesters', mine_b[0])  # users don't see who else asked

        all_admin = admin.get('/api/retro/requests').get_json()['requests']
        self.assertEqual([p['user'] for p in all_admin[0]['requesters']], ['examplea', 'exampleb'])
        self.assertEqual(len(self.sent), 2)

    def test_withdraw_removes_only_own_backing(self):
        a, b = self.client_for(self.user_a), self.client_for(self.user_b)
        rid = self._create(a, 'Example Missing').get_json()['request']['id']
        self._create(b, 'Example Missing')

        r = a.delete(f'/api/retro/requests/{rid}')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['deleted'])
        self.assertEqual(a.get('/api/retro/requests').get_json()['requests'], [])

        r = b.delete(f'/api/retro/requests/{rid}')
        self.assertTrue(r.get_json()['deleted'])
        with self.app.app_context():
            self.assertIsNone(db.session.get(req_mod.RetroRequest, rid))

    def test_admin_actions_and_permissions(self):
        a, admin = self.client_for(self.user_a), self.client_for(self.admin_id)
        rid = self._create(a, 'Example Missing').get_json()['request']['id']
        self.assertEqual(a.patch(f'/api/retro/requests/{rid}', json={'status': 'approved'}).status_code, 403)
        r = admin.patch(f'/api/retro/requests/{rid}', json={'status': 'approved', 'admin_note': 'Buying it'})
        self.assertEqual(r.get_json()['request']['status'], 'approved')
        self.assertEqual(r.get_json()['request']['admin_note'], 'Buying it')
        self.assertEqual(admin.patch(f'/api/retro/requests/{rid}', json={'status': 'bogus'}).status_code, 400)
        self.assertEqual(a.delete(f'/api/retro/requests/{rid}').status_code, 403)  # no longer pending
        self.assertEqual(admin.delete(f'/api/retro/requests/{rid}').status_code, 200)
        self.assertEqual(a.get('/retro/requests').status_code, 200)

    def test_unseen_count_and_mark_seen(self):
        a, admin = self.client_for(self.user_a), self.client_for(self.admin_id)
        self.assertEqual(a.get('/api/retro/requests/unseen-count').status_code, 403)
        r1 = self._create(a, 'Example One').get_json()['request']['id']
        self._create(a, 'Example Two')
        self.assertEqual(admin.get('/api/retro/requests/unseen-count').get_json()['count'], 2)

        listed = {r['id']: r['seen'] for r in admin.get('/api/retro/requests').get_json()['requests']}
        self.assertEqual(set(listed.values()), {False})

        self.assertEqual(admin.post('/api/retro/requests/mark-seen', json={'request_ids': [r1]}).get_json()['marked'], 1)
        self.assertEqual(admin.get('/api/retro/requests/unseen-count').get_json()['count'], 1)
        self.assertEqual(admin.post('/api/retro/requests/mark-seen', json={}).get_json()['marked'], 1)
        self.assertEqual(admin.get('/api/retro/requests/unseen-count').get_json()['count'], 0)

    def test_scan_fulfils_matching_request_on_same_platform(self):
        a = self.client_for(self.user_a)
        self._create(a, 'Example Missing', platform='gba')
        self._create(a, 'Example Missing', platform='gb')
        with open(os.path.join(self.roms_root, 'gba', 'Example Missing (Europe) [!].gba'), 'wb') as f:
            f.write(b'n')
        with self.app.app_context():
            roms_mod.scan_roms()
            by_platform = {r.platform: r for r in req_mod.RetroRequest.query.all()}
            self.assertEqual(by_platform['gba'].status, 'fulfilled')
            self.assertIsNotNone(by_platform['gba'].rom_id)
            self.assertEqual(by_platform['gb'].status, 'pending')
        self.assertTrue(any('fulfilled' in m for m in self.sent))

    def test_deleting_user_removes_their_backing(self):
        a = self.client_for(self.user_a)
        rid = self._create(a, 'Example Missing').get_json()['request']['id']
        with self.app.app_context():
            db.session.execute(db.text('PRAGMA foreign_keys=ON'))
            db.session.delete(db.session.get(User, self.user_a))
            db.session.commit()
            self.assertEqual(req_mod.RetroRequestUser.query.filter_by(request_id=rid).count(), 0)


if __name__ == '__main__':
    unittest.main()
