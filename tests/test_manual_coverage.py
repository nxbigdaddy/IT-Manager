import importlib.util
from pathlib import Path
from html.parser import HTMLParser
from itmanager.schema import MODULES
ROOT=Path(__file__).resolve().parents[1]/'manual'
class Images(HTMLParser):
    def __init__(self):super().__init__();self.images=[]
    def handle_starttag(self,tag,attrs):
        if tag=='img':self.images.append(dict(attrs))

def test_business_fields_and_images_all_covered():
    spec=importlib.util.spec_from_file_location('business_content',ROOT/'business_content.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    assert set(mod.BUSINESS)==set(MODULES)
    for key,(_,fields) in MODULES.items():
        assert set(mod.BUSINESS[key]['fields'])==set(fields)
        html=(ROOT/('business-'+key+'.html')).read_text(encoding='utf-8')
        assert all(field in html for field in fields)
        assert f'images/page-{key}.png' in html
    project=(ROOT/'itmanager.hhp').read_text(encoding='utf-8')
    for page in ROOT.glob('*.html'):
        parser=Images();parser.feed(page.read_text(encoding='utf-8'))
        for attrs in parser.images:
            assert attrs.get('alt')
            image=ROOT/attrs['src'];assert image.is_file() and image.stat().st_size>1000
            assert attrs['src'] in project
    css=(ROOT/'style.css').read_text(encoding='utf-8')
    assert 'A4 landscape' in css and 'page-break-inside:avoid' in css
