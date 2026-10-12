"""Offline manual integrity checks; no GUI, network, or CHM compiler required."""
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / 'manual'


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links, self.ids, self.local_topics = [], set(), []
        self.title = self.charset = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.title |= tag == 'title'
        if tag == 'meta':
            self.charset |= attrs.get('charset', '').lower() == 'utf-8'
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        for key in ('href', 'src'):
            if key in attrs:
                self.links.append(attrs[key])
        if tag == 'param' and attrs.get('name', '').lower() == 'local':
            self.local_topics.append(attrs.get('value', ''))


def test_manual_utf8_relative_links_and_fragments():
    pages = sorted(MANUAL.glob('*.html'))
    assert len(pages) == 53
    docs = {p.resolve(): Document(p.read_text(encoding='utf-8')) for p in pages}
    for page, doc in docs.items():
        assert doc.title and doc.charset, page.name
        assert doc.links, page.name
        for link in doc.links:
            url = urlsplit(link)
            assert not url.scheme and not url.netloc, (page.name, link)
            assert not url.path.startswith(('/', '\\')), (page.name, link)
            target = (page.parent / unquote(url.path)).resolve() if url.path else page
            assert target.is_relative_to(MANUAL.resolve()), (page.name, link)
            assert target.is_file(), (page.name, link)
            if url.fragment:
                assert target in docs and unquote(url.fragment) in docs[target].ids
    css = (MANUAL / 'style.css').read_text(encoding='utf-8')
    assert not re.search(r'@import|url\s*\(', css, re.I)


def test_chm_project_topics_and_no_compiled_deliverable():
    pages = {p.name for p in MANUAL.glob('*.html')}
    project = (MANUAL / 'itmanager.hhp').read_text(encoding='utf-8')
    files = set(project.split('[FILES]\n', 1)[1].splitlines())
    assert pages <= files
    assert 'Default topic=index.html' in project
    assert 'Language=0x804' in project
    for ext in ('hhc', 'hhk'):
        text = (MANUAL / ('itmanager.' + ext)).read_text(encoding='utf-8')
        doc = Document(text)
        assert set(doc.local_topics) == pages
        assert '&#' in text  # Chinese names use numeric entities for old HHC.
    assert not list(MANUAL.glob('*.chm'))


def test_readme_relative_links_resolve():
    text = (ROOT / 'README.md').read_text(encoding='utf-8')
    for link in re.findall(r'\]\(<([^>]+)>\)', text):
        url = urlsplit(link)
        assert not url.scheme and not url.netloc
        assert (ROOT / unquote(url.path)).is_file(), link


def test_manual_states_critical_product_boundaries():
    text = '\n'.join(p.read_text(encoding='utf-8') for p in MANUAL.glob('*.html'))
    for phrase in ('单可信管理员', 'SQLite', 'DPAPI', 'SMTP仅发送', '未编译', 'uncertain', '重复ID', '不执行', '{{content}}'):
        assert phrase in text
