"""Five small end-to-end checks. Tests wait for results; ingestion never polls."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import boto3
import requests
from client import OpenCloud, hits, item_url, properties, search, INDEX

api = OpenCloud()
run_id = secrets.token_hex(3)
report = {'run': run_id, 'passed': [], 'documents': []}
state_path = Path('/state/events.json')


def wait(condition, description, timeout=180):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = condition()
            if result:
                return result
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise AssertionError(f'Timeout: {description}; {last_error}; consumer: {state_path.read_text()[-1200:]}')


def passed(label):
    report['passed'].append(label)
    Path('/state/test.json').write_text(json.dumps(report, indent=2))
    print('PASS ' + label, flush=True)


def snapshot(fid):
    return [hit['_source'] for hit in hits(fid)]


def indexed(fid, text):
    chunks = snapshot(fid)
    return chunks and text in ' '.join(x['text'] for x in chunks) and chunks


def access(user, fid):
    response = user.request('GET', '/dav/spaces/' + fid)
    assert response.status_code in (200, 401, 403, 404), (response.status_code, response.text[:200])
    return response.status_code == 200


def vector_access(user_id, fid):
    return search('POST', f'/{INDEX}/_count', json={'query': {'bool': {'filter': [
        {'term': {'document_id': fid}}, {'term': {'allowed_user_ids': user_id}}]}}})['count'] > 0


def share(fid, group):
    return api.call('POST', item_url(fid) + '/invite', json={
        'recipients': [{'@libre.graph.recipient.type': 'group', 'objectId': group['id']}],
        'roles': ['b1e2218d-eef8-4d4c-b82d-0f1a1b48f3b5']})['value'][0]


def member(group, user, add):
    endpoint = f'/graph/v1.0/groups/{group["id"]}/members'
    if add:
        api.call('POST', endpoint + '/$ref', json={'@odata.id': '/graph/v1.0/users/' + user['id']})
    else:
        api.call('DELETE', endpoint + '/' + user['id'] + '/$ref')


wait(lambda: state_path.exists() and json.loads(state_path.read_text()).get('ready'), 'consumer NATS pronto')
drive = next(d for d in api.items('/graph/v1.0/me/drives') if d['driveType'] == 'personal')
users, clients, groups = [], [], []
password = secrets.token_urlsafe(18) + 'Aa1!'
for letter in ('a', 'b'):
    username = f'rag-{run_id}-{letter}'
    user = api.call('POST', '/graph/v1.0/users', json={
        'onPremisesSamAccountName': username, 'displayName': f'RAG {run_id} {letter.upper()}',
        'mail': username + '@example.test', 'accountEnabled': True,
        'passwordProfile': {'password': password}})
    users.append(user)
    clients.append(OpenCloud(username, password))
    group = api.call('POST', '/graph/v1.0/groups', json={'displayName': username})
    groups.append(group)
    member(group, user, True)
# Keep test login credentials local, never in console/evidence.
Path('/state/test-users.json').write_text(json.dumps({'users': users, 'password': password, 'groups': groups}, indent=2))
os.chmod('/state/test-users.json', 0o600)
contents = [f'Contratto {run_id}: il pagamento deve avvenire entro trenta giorni.',
            f'Progetto {run_id}: la consegna tecnica richiede revisione del responsabile.',
            f'Personale {run_id}: il piano ferie viene concordato con il gruppo.']
for number, text in enumerate(contents, 1):
    name = f'rag-{run_id}-{number}.txt'
    response = api.request('PUT', '/dav/spaces/' + drive['id'] + '/' + name, data=text.encode())
    response.raise_for_status()
    fid = response.headers['OC-FileId']
    report['documents'].append({'id': fid, 'name': name})
    download = api.request('GET', '/dav/spaces/' + fid)
    download.raise_for_status()
    assert download.content == text.encode()
# Prove actual bytes reached MinIO, not a second copy placed there by the test.
s3 = boto3.client('s3', endpoint_url='http://minio:9000', region_name='us-east-1',
                  aws_access_key_id=os.environ['MINIO_ROOT_USER'],
                  aws_secret_access_key=os.environ['MINIO_ROOT_PASSWORD'])
objects = s3.list_objects_v2(Bucket='opencloud-documents').get('Contents', [])
blobs = [s3.get_object(Bucket='opencloud-documents', Key=o['Key'])['Body'].read() for o in objects]
assert all(t.encode() in blobs for t in contents)
passed('1/5 Tre documenti caricati via OpenCloud e conservati in MinIO')
ids = [doc['id'] for doc in report['documents']]
for fid, text in zip(ids, contents):
    chunks = wait(lambda: indexed(fid, text), 'chunk creati su evento FileUploaded')
    assert all(len(c['vector']) == 384 for c in chunks)
passed('2/5 Eventi nativi push -> chunk con embedding reali in OpenSearch')
shares = [share(ids[0], groups[0]), share(ids[1], groups[0]),
          share(ids[1], groups[1]), share(ids[2], groups[1])]
for i, user in enumerate(users):
    expected = [True, True, False] if i == 0 else [False, True, True]
    for fid, allowed in zip(ids, expected):
        wait(lambda: vector_access(user['id'], fid) == allowed, 'ACL iniziali nel vector DB')
        assert access(clients[i], fid) == allowed
before = snapshot(ids[1])
runs_before = json.loads(state_path.read_text())['documents'][ids[1]]['embedding_runs']
for role in ('2d00ce52-1fc2-4dbc-8b95-a73b73395f5a', 'b1e2218d-eef8-4d4c-b82d-0f1a1b48f3b5'):
    api.call('PATCH', item_url(ids[1]) + '/permissions/' + shares[1]['id'], json={'roles': [role]})
    wait(lambda: any(g['id'] == groups[0]['id'] and role in g['roles']
                     for c in snapshot(ids[1]) for g in c['acl']), 'modifica ruolo sulla condivisione')
api.call('DELETE', item_url(ids[1]) + '/permissions/' + shares[1]['id'])
wait(lambda: not vector_access(users[0]['id'], ids[1]), 'revoca ACL nel vector DB')
wait(lambda: not access(clients[0], ids[1]), 'revoca ACL OpenCloud')
share(ids[1], groups[0])
wait(lambda: vector_access(users[0]['id'], ids[1]), 'nuova concessione ACL')
member(groups[0], users[0], False)
wait(lambda: not vector_access(users[0]['id'], ids[1]), 'rimozione appartenenza al gruppo')
wait(lambda: not access(clients[0], ids[1]), 'revoca accesso dopo rimozione dal gruppo')
member(groups[0], users[0], True)
wait(lambda: vector_access(users[0]['id'], ids[1]), 'aggiunta appartenenza al gruppo')
wait(lambda: access(clients[0], ids[1]), 'accesso ripristinato dopo aggiunta al gruppo')
after = snapshot(ids[1])
assert [c['vector'] for c in before] == [c['vector'] for c in after]
assert json.loads(state_path.read_text())['documents'][ids[1]]['embedding_runs'] == runs_before
passed('3/5 ACL e gruppi: accessi coerenti, revoca e ripristino senza ricalcolare embedding')
first = ids[0]
old_version = snapshot(first)[0]['version']
# Distinct modification times make the native version history clear in the UI.
time.sleep(2)
new_text = f'Contratto {run_id}: NUOVA VERSIONE, pagamento entro sessanta giorni.'
response = api.request('PUT', '/dav/spaces/' + first, data=new_text.encode())
response.raise_for_status()
chunks = wait(lambda: indexed(first, new_text), 'nuova versione su evento nativo')
assert all(c['version'] != old_version for c in chunks)
assert all(contents[0] not in c['text'] for c in chunks)
passed('4/5 Nuova versione: aggiornamento automatico e sostituzione dei chunk precedenti')
versions = api.request('PROPFIND', '/dav/meta/' + first + '/v', headers={'Depth': '1'})
versions.raise_for_status()
links = [x.text for x in ET.fromstring(versions.content).findall('.//{DAV:}href')]
old_link = None
for link in links[1:]:
    response = api.request('GET', link)
    if response.status_code == 200 and response.content == contents[0].encode():
        old_link = link
        break
assert old_link, 'Versione originale non trovata nella cronologia nativa'
response = api.request('COPY', old_link, headers={'Destination': api.url + '/dav/spaces/' + first})
response.raise_for_status()
wait(lambda: indexed(first, contents[0]), 'ripristino versione su evento FileVersionRestored')
assert api.request('GET', '/dav/spaces/' + first).content == contents[0].encode()
passed('5/5 Ripristino nativo della versione precedente e riallineamento automatico dei chunk')
report['event_types'] = sorted({x['type'] for x in json.loads(state_path.read_text())['received']})
required = {'events.FileUploaded', 'events.ShareCreated', 'events.ShareUpdated', 'events.ShareRemoved',
            'events.GroupMemberAdded', 'events.GroupMemberRemoved', 'events.FileVersionRestored'}
assert required <= set(report['event_types']), (required - set(report['event_types']))
Path('/state/test.json').write_text(json.dumps(report, indent=2))
print('5/5 test superati. Eventi verificati:', ', '.join(report['event_types']), flush=True)
