"""Retro game requests, separate from AeroFoil's Switch title requests.

A plain wishlist: users ask for games, admins approve, decline or fulfil them, and a
ROM library scan that finds a matching game closes the request automatically.
Several users can back the same request, and admins get an unseen-requests count.
Nothing here talks to indexers or download clients.
"""
import logging
import os
import re
import threading

import requests as http
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user

from app.auth import access_required, admin_account_created
from app.db import db, utc_now

logger = logging.getLogger('main')

WEBHOOK_URL = (os.environ.get('AEROFOIL_RETRO_REQUESTS_WEBHOOK')
               or os.environ.get('OWNFOIL_REQUESTS_WEBHOOK') or '').strip()
STATUSES = ('pending', 'approved', 'declined', 'fulfilled')
OPEN_STATUSES = ('pending', 'approved')
MAX_TITLE = 200
MAX_NOTE = 500


class RetroRequest(db.Model):
    __tablename__ = 'retro_requests'
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(32), nullable=False, index=True)
    title = db.Column(db.String(MAX_TITLE), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    admin_note = db.Column(db.String(MAX_NOTE))
    rom_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now, onupdate=utc_now)


class RetroRequestUser(db.Model):
    """One row per user backing a request, in the order they asked."""
    __tablename__ = 'retro_request_users'
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('retro_requests.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    note = db.Column(db.String(MAX_NOTE))
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    __table_args__ = (db.UniqueConstraint('request_id', 'user_id', name='uq_retro_request_users_request_user'),)

    request = db.relationship('RetroRequest', backref=db.backref(
        'requesters', lazy=True, cascade='all, delete-orphan', order_by='RetroRequestUser.created_at'))
    user = db.relationship('User')


class RetroRequestView(db.Model):
    """An admin has seen a request; drives the unseen badge."""
    __tablename__ = 'retro_request_views'
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey('retro_requests.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True)
    viewed_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    __table_args__ = (db.UniqueConstraint('request_id', 'user_id', name='uq_retro_request_views_request_user'),)

    request = db.relationship('RetroRequest', backref=db.backref('views', lazy=True, cascade='all, delete-orphan'))


requests_blueprint = Blueprint('retro_requests', __name__)


# ---- helpers ----

def normalize_title(title):
    """'Golden Sun (USA, Europe) [!]' -> 'golden sun'"""
    t = re.sub(r'[\(\[][^\)\]]*[\)\]]', ' ', (title or '').lower())
    t = t.replace('&', ' and ')
    t = re.sub(r'[^a-z0-9]+', ' ', t)
    return ' '.join(t.split())


def _is_admin():
    return bool(current_user.is_authenticated and getattr(current_user, 'is_admin', False))


def _username(user):
    return getattr(user, 'user', None) or 'deleted user'


def notify(message):
    """Fire-and-forget Discord-compatible webhook."""
    if not WEBHOOK_URL:
        return

    def _send():
        try:
            http.post(WEBHOOK_URL, json={'content': message}, timeout=10)
        except Exception as e:
            logger.warning(f'Retro request webhook failed: {e}')
    threading.Thread(target=_send, daemon=True).start()


def _requester_names(req):
    return [_username(link.user) for link in req.requesters]


def _to_dict(req, viewer_id=None, seen_ids=None):
    from app.roms import PLATFORMS
    names = _requester_names(req)
    mine = next((link for link in req.requesters if link.user_id == viewer_id), None)
    data = {
        'id': req.id,
        'title': req.title,
        'platform': req.platform,
        'platform_name': PLATFORMS.get(req.platform, (req.platform,))[0],
        'status': req.status,
        'admin_note': req.admin_note,
        'rom_id': req.rom_id,
        'requester_count': len(names),
        'is_mine': mine is not None,
        'my_note': mine.note if mine else None,
        'created_at': req.created_at.isoformat() if req.created_at else None,
    }
    if seen_ids is not None:
        data['requesters'] = [
            {'user': _username(link.user), 'note': link.note,
             'at': link.created_at.isoformat() if link.created_at else None}
            for link in req.requesters
        ]
        data['seen'] = req.id in seen_ids
    return data


def _seen_ids_for(user_id):
    rows = db.session.query(RetroRequestView.request_id).filter(RetroRequestView.user_id == user_id).all()
    return {r[0] for r in rows}


def _unseen_query(user_id):
    return (
        db.session.query(RetroRequest)
        .outerjoin(RetroRequestView, (RetroRequestView.request_id == RetroRequest.id)
                   & (RetroRequestView.user_id == user_id))
        .filter(RetroRequest.status == 'pending')
        .filter(RetroRequestView.id.is_(None))
    )


def fulfil_matching_requests():
    """Close open requests whose title+platform now match a ROM in the library."""
    from app.roms import Rom
    open_reqs = RetroRequest.query.filter(RetroRequest.status.in_(OPEN_STATUSES)).all()
    if not open_reqs:
        return 0
    by_key = {}
    for rom in Rom.query.all():
        by_key.setdefault((rom.platform, normalize_title(rom.name)), rom)
    closed = []
    for req in open_reqs:
        rom = by_key.get((req.platform, normalize_title(req.title)))
        if rom:
            req.status = 'fulfilled'
            req.rom_id = rom.id
            closed.append(req)
    if closed:
        db.session.commit()
        for req in closed:
            notify(f'✅ Retro request fulfilled: **{req.title}** ({req.platform}) is now in the library. '
                   f'Requested by {", ".join(_requester_names(req))}.')
        logger.info(f'Retro requests: {len(closed)} fulfilled by library scan')
    return len(closed)


# ---- page ----

@requests_blueprint.route('/retro/requests')
@access_required('shop')
def retro_requests_page():
    from app.roms import PLATFORMS
    return render_template('retro_requests.html', title='Retro',
                           platforms=[(k, v[0]) for k, v in PLATFORMS.items()],
                           is_admin=_is_admin(),
                           admin_account_created=admin_account_created())


# ---- API ----

@requests_blueprint.get('/api/retro/requests')
@access_required('shop')
def list_retro_requests():
    q = RetroRequest.query
    if not _is_admin():
        q = q.join(RetroRequestUser, RetroRequestUser.request_id == RetroRequest.id) \
             .filter(RetroRequestUser.user_id == current_user.id)
    status = request.args.get('status')
    if status in STATUSES:
        q = q.filter(RetroRequest.status == status)
    rows = q.order_by(RetroRequest.created_at.desc()).limit(500).all()
    seen_ids = _seen_ids_for(current_user.id) if _is_admin() else None
    return jsonify({'success': True,
                    'requests': [_to_dict(r, viewer_id=current_user.id, seen_ids=seen_ids) for r in rows]})


@requests_blueprint.post('/api/retro/requests')
@access_required('shop')
def create_retro_request():
    from app.roms import PLATFORMS, Rom
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    platform = (data.get('platform') or '').strip()
    note = (data.get('note') or '').strip()[:MAX_NOTE] or None
    if not title or len(title) > MAX_TITLE:
        return jsonify({'success': False, 'message': 'Title is required (max 200 characters).'}), 400
    if platform not in PLATFORMS:
        return jsonify({'success': False, 'message': 'Unknown platform.'}), 400

    wanted = normalize_title(title)
    if not wanted:
        return jsonify({'success': False, 'message': 'Title is required (max 200 characters).'}), 400

    for rom in Rom.query.filter_by(platform=platform).all():
        if normalize_title(rom.name) == wanted:
            return jsonify({'success': False, 'message': f'"{rom.name}" is already in the library.',
                            'rom_id': rom.id}), 409

    existing = next((r for r in RetroRequest.query.filter(RetroRequest.platform == platform,
                                                          RetroRequest.status.in_(OPEN_STATUSES)).all()
                     if normalize_title(r.title) == wanted), None)
    if existing is not None:
        if any(link.user_id == current_user.id for link in existing.requesters):
            return jsonify({'success': True, 'joined': False, 'message': 'You already requested this game.',
                            'request': _to_dict(existing, viewer_id=current_user.id)})
        db.session.add(RetroRequestUser(request_id=existing.id, user_id=current_user.id, note=note))
        db.session.commit()
        notify(f'➕ {_username(current_user)} also wants **{existing.title}** '
               f'({PLATFORMS[platform][0]}) - {len(existing.requesters)} requesters now.')
        return jsonify({'success': True, 'joined': True,
                        'message': 'Someone already requested this game - you have been added to it.',
                        'request': _to_dict(existing, viewer_id=current_user.id)})

    req = RetroRequest(title=title, platform=platform)
    db.session.add(req)
    db.session.flush()
    db.session.add(RetroRequestUser(request_id=req.id, user_id=current_user.id, note=note))
    db.session.commit()
    notify(f'🎮 New retro request from {_username(current_user)}: **{title}** ({PLATFORMS[platform][0]})')
    return jsonify({'success': True, 'joined': False, 'message': 'Request added.',
                    'request': _to_dict(req, viewer_id=current_user.id)}), 201


@requests_blueprint.patch('/api/retro/requests/<int:req_id>')
@access_required('admin')
def update_retro_request(req_id):
    req = db.session.get(RetroRequest, req_id)
    if not req:
        return jsonify({'success': False, 'message': 'Not found.'}), 404
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status is not None:
        if status not in STATUSES:
            return jsonify({'success': False, 'message': 'Invalid status.'}), 400
        if status != req.status:
            req.status = status
            if status in ('approved', 'declined', 'fulfilled'):
                icon = {'approved': '👍', 'declined': '❌', 'fulfilled': '✅'}[status]
                notify(f'{icon} Retro request **{req.title}** was {status}. '
                       f'Requested by {", ".join(_requester_names(req))}.')
    if 'admin_note' in data:
        req.admin_note = (data.get('admin_note') or '').strip()[:MAX_NOTE] or None
    if not db.session.query(RetroRequestView).filter_by(request_id=req.id, user_id=current_user.id).first():
        db.session.add(RetroRequestView(request_id=req.id, user_id=current_user.id))
    db.session.commit()
    return jsonify({'success': True, 'request': _to_dict(req, viewer_id=current_user.id,
                                                         seen_ids=_seen_ids_for(current_user.id))})


@requests_blueprint.delete('/api/retro/requests/<int:req_id>')
@access_required('shop')
def delete_retro_request(req_id):
    """Admins delete the request; users withdraw their own backing while it is pending."""
    req = db.session.get(RetroRequest, req_id)
    if not req:
        return jsonify({'success': False, 'message': 'Not found.'}), 404
    if _is_admin():
        db.session.delete(req)
        db.session.commit()
        return jsonify({'success': True, 'deleted': True})

    link = next((l for l in req.requesters if l.user_id == current_user.id), None)
    if link is None or req.status != 'pending':
        return jsonify({'success': False, 'message': 'You can only withdraw your own pending requests.'}), 403
    db.session.delete(link)
    db.session.flush()
    deleted = not RetroRequestUser.query.filter_by(request_id=req.id).count()
    if deleted:
        db.session.delete(req)
    db.session.commit()
    return jsonify({'success': True, 'deleted': deleted})


@requests_blueprint.get('/api/retro/requests/unseen-count')
@access_required('admin')
def retro_unseen_count():
    return jsonify({'success': True, 'count': int(_unseen_query(current_user.id).count())})


@requests_blueprint.post('/api/retro/requests/mark-seen')
@access_required('admin')
def retro_mark_seen():
    """Mark the given request ids (or every unseen pending request) as seen by this admin."""
    data = request.get_json(silent=True) or {}
    ids = data.get('request_ids')
    if ids:
        try:
            ids = {int(x) for x in ids}
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': 'Invalid request_ids.'}), 400
        already = _seen_ids_for(current_user.id)
        valid = {r[0] for r in db.session.query(RetroRequest.id).filter(RetroRequest.id.in_(ids)).all()}
        targets = valid - already
    else:
        targets = {r.id for r in _unseen_query(current_user.id).all()}
    for rid in targets:
        db.session.add(RetroRequestView(request_id=rid, user_id=current_user.id))
    db.session.commit()
    return jsonify({'success': True, 'marked': len(targets)})
