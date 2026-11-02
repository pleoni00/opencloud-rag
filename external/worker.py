"""Native NATS push -> native OpenCloud APIs -> vector chunks and ACL metadata."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy
from client import OpenCloud, INDEX, search, properties, item_url

api = OpenCloud()
path = Path('/state/events.json')
state = json.loads(path.read_text()) if path.exists() else {'documents': {}, 'received': []}
model = None
CONTENT = {'events.FileUploaded', 'events.FileVersionRestored', 'events.ItemRestored'}
ACL = {'events.ShareCreated', 'events.ShareUpdated', 'events.ShareRemoved', 'events.ShareExpired',
       'events.GroupMemberAdded', 'events.GroupMemberRemoved', 'events.GroupDeleted'}
DELETE = {'events.ItemTrashed', 'events.ItemPurged'}


def save():
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(path)


def resource_id(resource):
    drive = resource['storage_id'] + '$' + resource['space_id']
    return drive + '!' + resource['opaque_id']


def policy(doc_id, owner_id):
    permissions = api.items(item_url(doc_id) + '/permissions')
    grants = [{'type': 'user', 'id': owner_id, 'roles': ['owner']}]
    users = {owner_id}
    roles = api.call('GET', '/graph/v1beta1/roleManagement/permissions/roleDefinitions')
    readable_roles = {role['id'] for role in roles if any(
        'libre.graph/driveItem/content/read' in p.get('allowedResourceActions', [])
        for p in role.get('rolePermissions', []))}
    for permission in permissions:
        # Public links are deliberately outside the authenticated PoC access model.
        for grantee in ([permission['grantedToV2']] if permission.get('grantedToV2') else permission.get('grantedToIdentitiesV2', [])):
            for kind in ('user', 'group'):
                if kind not in grantee:
                    continue
                identity = grantee[kind]['id']
                grant = {'type': kind, 'id': identity, 'roles': permission.get('roles', []),
                         'actions': permission.get('@libre.graph.permissions.actions', [])}
                if grant not in grants:
                    grants.append(grant)
                # Only readable shares grant access to chunk text.
                actions = grant['actions']
                readable = ('libre.graph/driveItem/content/read' in actions or bool(set(grant['roles']) & readable_roles))
                if readable:
                    if kind == 'user':
                        users.add(identity)
                    else:
                        users.update(u['id'] for u in api.items(f'/graph/v1.0/groups/{identity}/members'))
    return grants, sorted(users)


def synchronize(doc_id, owner_id):
    global model
    meta = properties(api, '/dav/spaces/' + quote(doc_id, safe='$!'))
    if meta['folder'] or not (meta['name'] or '').lower().endswith('.txt'):
        return
    grants, users = policy(doc_id, owner_id)
    previous = state['documents'].get(doc_id, {})
    version = meta['etag']
    if version != previous.get('version'):
        response = api.request('GET', '/dav/spaces/' + quote(doc_id, safe='$!'))
        response.raise_for_status()
        text = response.content.decode('utf-8')
        words = text.split()
        chunks = [' '.join(words[i:i + 160]) for i in range(0, len(words), 160)] or ['']
        if model is None:
            from fastembed import TextEmbedding
            model = TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                                  specific_model_path='/models/manual', threads=2)
        vectors = list(model.embed(chunks, batch_size=4))
        if properties(api, '/dav/spaces/' + quote(doc_id, safe='$!'))['etag'] != version:
            raise RuntimeError('Versione cambiata durante la lettura')
        operations = []
        for number, (chunk, vector) in enumerate(zip(chunks, vectors)):
            key = hashlib.sha256((doc_id + version + str(number)).encode()).hexdigest()
            operations.extend([{'index': {'_index': INDEX, '_id': key}}, {
                'document_id': doc_id, 'version': version, 'name': meta['name'], 'ordinal': number,
                'text': chunk, 'vector': vector.tolist(), 'acl': grants, 'allowed_user_ids': users}])
        result = search('POST', '/_bulk?refresh=wait_for',
                        data='\n'.join(json.dumps(x) for x in operations) + '\n',
                        headers={'Content-Type': 'application/x-ndjson'})
        if result.get('errors'):
            raise RuntimeError('Errore scrittura chunk')
        search('POST', f'/{INDEX}/_delete_by_query?refresh=true', json={'query': {'bool': {
            'filter': [{'term': {'document_id': doc_id}}],
            'must_not': [{'term': {'version': version}}]}}})
        previous['embedding_runs'] = previous.get('embedding_runs', 0) + 1
    else:
        search('POST', f'/{INDEX}/_update_by_query?refresh=true', json={
            'query': {'term': {'document_id': doc_id}},
            'script': {'source': 'ctx._source.acl=params.acl; ctx._source.allowed_user_ids=params.users; ctx._source.name=params.name',
                       'params': {'acl': grants, 'users': users, 'name': meta['name']}}})
    state['documents'][doc_id] = dict(previous, version=version, owner_id=owner_id, name=meta['name'])
    print('Sincronizzato', meta['name'], version, flush=True)


def process(verb, payload):
    if verb in ACL:
        # Re-read known documents only; no server crawl.
        for doc_id, doc in list(state['documents'].items()):
            synchronize(doc_id, doc['owner_id'])
    elif verb in CONTENT:
        ref = payload.get('Ref') or {}
        resource = ref.get('resource_id')
        if not resource:
            return
        if resource.get('space_id') != owner_id:
            return  # PoC scope: admin's personal space, no privileged filesystem reads.
        candidate = resource_id(resource)
        suffix = (ref.get('path') or '').removeprefix('./').strip('/')
        if suffix and suffix != '.':
            candidate += '/' + quote(suffix)
        meta = properties(api, '/dav/spaces/' + candidate)
        if meta['id']:
            synchronize(meta['id'], owner_id)
    elif verb in DELETE:
        resource = payload.get('ID')
        targets = [resource_id(resource)] if resource else []
        for doc_id in targets:
            search('POST', f'/{INDEX}/_delete_by_query?refresh=true',
                   json={'query': {'term': {'document_id': doc_id}}})
            state['documents'].pop(doc_id, None)


async def run():
    global owner_id
    for attempt in range(120):
        try:
            owner_id = await asyncio.to_thread(lambda: api.call('GET', '/graph/v1.0/me')['id'])
            break
        except Exception:
            await asyncio.sleep(2)
    else:
        raise RuntimeError('OpenCloud non pronto')
    import requests
    try:
        search('GET', '/' + INDEX)
    except requests.HTTPError as exc:
        if exc.response.status_code != 404:
            raise
        search('PUT', '/' + INDEX, json={'settings': {'index': {'knn': True, 'number_of_shards': 1, 'number_of_replicas': 0}},
            'mappings': {'properties': {
                'document_id': {'type': 'keyword'}, 'version': {'type': 'keyword'},
                'allowed_user_ids': {'type': 'keyword'}, 'ordinal': {'type': 'integer'},
                'acl': {'type': 'object', 'enabled': False},
                'vector': {'type': 'knn_vector', 'dimension': 384,
                    'method': {'name': 'hnsw', 'engine': 'lucene', 'space_type': 'cosinesimil'}}}}})
    nc = await nats.connect(os.environ['NATS_URL'], name='opencloud-rag-poc', max_reconnect_attempts=-1)
    js = nc.jetstream()
    async def receive(message):
        envelope = json.loads(message.data)
        metadata = envelope['Metadata']
        verb, event_id = metadata.get('eventtype'), metadata.get('eventid')
        if verb not in CONTENT | ACL | DELETE:
            await message.ack()
            return
        payload = json.loads(base64.b64decode(envelope['Payload']))
        try:
            await asyncio.to_thread(process, verb, payload)
            state['received'] = (state['received'] + [{'id': event_id, 'type': verb, 'at': time.time()}])[-200:]
            state.pop('error', None)
            save()
            await message.ack()
        except Exception as exc:
            state['error'] = {'event': verb, 'message': str(exc)}
            save()
            print('Evento da ritentare:', verb, str(exc), flush=True)
            await message.nak(delay=5)
    await js.subscribe('main-queue', stream='main-queue', durable='rag-poc', manual_ack=True,
                       config=ConsumerConfig(deliver_policy=DeliverPolicy.ALL, ack_wait=180, max_ack_pending=1), cb=receive)
    state['ready'] = True
    save()
    print('Pronto: sottoscrizione NATS push attiva.', flush=True)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(run())
