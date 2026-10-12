"""Version contract across package, service health, docs and changelog."""
import re
from pathlib import Path

from itmanager import __version__

ROOT = Path(__file__).resolve().parents[1]


def version_tuple(value):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', value)
    assert match, value
    return tuple(map(int, match.groups()))


def test_package_version_is_current_and_semver():
    assert version_tuple(__version__) >= (0, 9, 0)


def test_readme_and_changelog_state_current_version_and_capabilities():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    changelog = (ROOT / '更新.md').read_text(encoding='utf-8')
    assert __version__ in readme
    assert f'v{__version__}' in changelog
    for phrase in ('证据附件', '全局搜索', '托盘提醒', '备份包'):
        assert phrase in readme + changelog


def test_manual_has_current_version_and_expected_new_topics():
    manual = (ROOT / 'manual' / 'index.html').read_text(encoding='utf-8')
    all_text = ''.join(path.read_text(encoding='utf-8') for path in (ROOT / 'manual').glob('*.html'))
    assert __version__ in manual
    for phrase in ('完整备份包', '全局检索', '系统托盘', '版本不一致'):
        assert phrase in all_text


def test_no_hardcoded_older_product_versions_in_runtime_source():
    patterns = []
    for path in (ROOT / 'itmanager').glob('*.py'):
        text = path.read_text(encoding='utf-8')
        if path.name == '__init__.py':
            continue
        for match in re.finditer(r"version['\"]?\s*[:=]\s*['\"](\d+\.\d+\.\d+)", text, re.I):
            if match.group(1) != __version__:
                patterns.append((path.name, match.group(1)))
    assert patterns == []
