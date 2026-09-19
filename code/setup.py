from setuptools import setup

APP = ['network-doctor-menu.py']
OPTIONS = {
    'argv_emulation': True,
    'plist': {
        'CFBundleName': '网络助手',
        'CFBundleDisplayName': '网络助手',
        'CFBundleIdentifier': 'com.wangxinlei.networkdoctor',
        'CFBundleVersion': '2.0.0',
        'CFBundleShortVersionString': '2.0.0',
        'NSHighResolutionCapable': True,
        'LSUIElement': True,
        'NSHumanReadableCopyright': 'Copyright (c) 2026 wangxinlei',
    },
    'packages': ['rumps', 'objc'],
    'strip': False,
    'optimize': 0,
    'compressed': True,
}

setup(
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
