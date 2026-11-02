"""Only native Graph/WebDAV APIs, plus the PoC vector index."""
import os
import time
from urllib.parse import urlsplit
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
INDEX = 'opencloud-chunks'

class OpenCloud:
    def __init__(self, user='admin', password=None):
        self.url = os.environ.get('OC_URL', 'https://opencloud:9200')
        self.session = requests.Session()
        self.session.verify = False  # local self-signed certificate
        self.session.auth = (user, password or os.environ['OC_ADMIN_PASSWORD'])

    def request(self, method, path, **kwargs):
        if path.startswith('http'):
            parsed = urlsplit(path)
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
        for attempt in range(4):
            try:
                response = self.session.request(method, self.url + path, timeout=30, **kwargs)
                if method == 'GET' and response.status_code in (502, 503, 504) and attempt < 3:
                    time.sleep(attempt + 1)
                    continue
                return response
            except (requests.ConnectionError, requests.Timeout):
                if method != 'GET' or attempt == 3:
                    raise
                time.sleep(attempt + 1)

    def call(self, method, path, **kwargs):
        response = self.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def items(self, path):
        result = []
        while path:
            data = self.call('GET', path)
            if isinstance(data, list):
                result.extend(data)
                break
            result.extend(data.get('value', []))
            path = data.get('@odata.nextLink')
        return result


def search(method, path, **kwargs):
    response = requests.request(method, os.environ.get('OPENSEARCH_URL', 'http://opensearch:9200') + path,
                                timeout=60, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def hits(document_id):
    return search('POST', f'/{INDEX}/_search', json={
        'size': 100, 'query': {'term': {'document_id': document_id}},
        'sort': [{'ordinal': 'asc'}]})['hits']['hits']


def properties(api, path):
    from xml.etree import ElementTree as ET
    response = api.request('PROPFIND', path, headers={'Depth': '0'}, data='<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>')
    response.raise_for_status()
    root = ET.fromstring(response.content)
    def value(tag):
        return root.findtext('.//' + tag)
    return {'id': value('{http://owncloud.org/ns}fileid'),
            'etag': value('{DAV:}getetag'), 'name': value('{DAV:}displayname'),
            'folder': root.find('.//{DAV:}collection') is not None}


def item_url(document_id):
    return '/graph/v1beta1/drives/' + document_id.split('!')[0] + '/items/' + document_id
