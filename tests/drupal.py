#!/usr/bin/env python3
"""Exercise real Drupal forms through Nginx, FastCGI and Polychrome."""
import argparse
import base64
from html.parser import HTMLParser
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.images = []
        self.form = None
        self.textarea = None
        self.select = None
        self.option = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'img':
            self.images.append(attrs.get('src', ''))
        if tag == 'form':
            self.form = {'id': attrs.get('id'), 'action': attrs.get('action', ''), 'fields': {}}
            self.forms.append(self.form)
        elif self.form and tag == 'input':
            name, kind = attrs.get('name'), attrs.get('type', 'text')
            if name and kind not in ('submit', 'file', 'button') and 'disabled' not in attrs:
                if kind not in ('checkbox', 'radio') or 'checked' in attrs:
                    self.form['fields'][name] = attrs.get('value', '')
        elif self.form and tag == 'textarea':
            self.textarea = attrs.get('name')
            if self.textarea:
                self.form['fields'][self.textarea] = ''
        elif self.form and tag == 'select':
            self.select = attrs.get('name')
        elif self.form and self.select and tag == 'option':
            if 'selected' in attrs or self.select not in self.form['fields']:
                self.form['fields'][self.select] = attrs.get('value', '')

    def handle_data(self, data):
        if self.form and self.textarea:
            self.form['fields'][self.textarea] += data

    def handle_endtag(self, tag):
        if tag == 'form': self.form = None
        if tag == 'textarea': self.textarea = None
        if tag == 'select': self.select = None


class Browser:
    def __init__(self, base):
        self.base = base
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def get(self, path):
        response = self.opener.open(urllib.parse.urljoin(self.base, path), timeout=40)
        return response.url, response.read().decode()

    def submit(self, url, html, form_prefix, changes, files=None):
        form = next((f for f in Page(html).forms if (f['id'] or '').startswith(form_prefix)), None)
        if not form:
            raise AssertionError(f'Missing {form_prefix} at {url}; forms={[f["id"] for f in Page(html).forms]}')
        fields = form['fields'] | changes
        if files:
            boundary = 'polychrome-' + uuid.uuid4().hex
            chunks = []
            for name, value in fields.items():
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
            for name, (filename, content_type, data) in files.items():
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode() + data + b'\r\n')
            chunks.append(f'--{boundary}--\r\n'.encode())
            body = b''.join(chunks)
            content_type = 'multipart/form-data; boundary=' + boundary
        else:
            body = urllib.parse.urlencode(fields).encode()
            content_type = 'application/x-www-form-urlencoded'
        target = urllib.parse.urljoin(url, form['action'])
        response = self.opener.open(urllib.request.Request(target, data=body, headers={'Content-Type': content_type}), timeout=40)
        return response.url, response.read().decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--anonymous-only', action='store_true')
    args = parser.parse_args()
    browser = Browser(args.base_url)
    deadline = time.monotonic() + 40
    while True:
        try:
            _, html = browser.get('/')
            if 'Polychrome integration' not in html:
                raise AssertionError('Drupal homepage did not contain the configured site name')
            break
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(.2)
    print('Drupal anonymous homepage passed', flush=True)
    if args.anonymous_only:
        return
    url, html = browser.get('/user/login')
    url, html = browser.submit(url, html, 'user-login-form', {'name': 'admin', 'pass': 'polychrome-example', 'op': 'Log in'})
    if not any(cookie.name.startswith(('SESS', 'SSESS')) for cookie in browser.cookies):
        raise AssertionError('Login did not establish a Drupal session')
    browser.get('/admin/content')
    print('Drupal login and authenticated administration passed', flush=True)

    title = 'Polychrome article ' + uuid.uuid4().hex[:8]
    url, html = browser.get('/node/add/article')
    # A valid one-pixel PNG is sufficient to test PHP uploads and Drupal's
    # managed-file widget without external image generators or browser tooling.
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
    fields = {'title[0][value]': title, 'body[0][value]': 'Created through Polychrome.',
              'body[0][format]': 'basic_html', 'field_image_0_upload_button': 'Upload'}
    url, html = browser.submit(url, html, 'node-article-form', fields,
                              {'files[field_image_0]': ('polychrome-upload.png', 'image/png', png)})
    fields.pop('field_image_0_upload_button')
    fields.update({'field_image[0][alt]': 'Polychrome upload', 'op': 'Save'})
    url, html = browser.submit(url, html, 'node-article-form', fields)
    match = re.search(r'/node/(\d+)', url)
    if not match or title not in html:
        raise AssertionError('Article creation failed: ' + re.sub('<[^>]+>', ' ', html)[-3000:])
    node = '/node/' + match.group(1)
    images = [image for image in Page(html).images if 'polychrome-upload' in image]
    if not images:
        raise AssertionError('Created article did not contain its uploaded image')
    with urllib.request.urlopen(urllib.parse.urljoin(url, images[0]), timeout=40) as response:
        if response.status != 200 or not response.read():
            raise AssertionError('Uploaded image could not be served')
    print('Drupal content creation and image upload passed', flush=True)

    url, html = browser.get(node + '/edit')
    url, html = browser.submit(url, html, 'node-article-edit-form', {
        'title[0][value]': title + ' edited', 'body[0][value]': 'Edited through Polychrome.',
        'body[0][format]': 'basic_html', 'op': 'Save'})
    if title + ' edited' not in html:
        raise AssertionError('Edited article was not rendered')
    _, anonymous = Browser(args.base_url).get(node)
    if title + ' edited' not in anonymous:
        raise AssertionError('Anonymous visitor could not read published article')
    print('Drupal content editing and anonymous rendering passed', flush=True)

    url, html = browser.get('/user/logout')
    if any((form['id'] or '').startswith('user-logout-confirm') for form in Page(html).forms):
        browser.submit(url, html, 'user-logout-confirm', {'op': 'Log out'})
    try:
        browser.get('/admin/content')
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    else:
        raise AssertionError('Administrative access persisted after logout')
    print('Drupal logout passed', flush=True)


if __name__ == '__main__':
    main()
