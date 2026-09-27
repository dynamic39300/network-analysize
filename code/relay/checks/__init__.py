#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NetCare 检测插件注册表
自动发现并注册所有检测插件
"""

import os
import importlib
from .base import BaseCheck, register, get_registry

# 复用 base 中的注册表
_REGISTRY = get_registry()
_LOADED = False
_LOAD_ERRORS = {}


def get_all_checks():
    """获取所有已注册的检测插件类"""
    _ensure_loaded()
    return dict(_REGISTRY)


def get_check(name):
    """按名称获取检测插件类"""
    _ensure_loaded()
    return _REGISTRY.get(name)


def create_instances(config=None, runner=None):
    """创建所有启用的检测插件实例"""
    _ensure_loaded()
    instances = []
    for name, cls in _REGISTRY.items():
        instance = cls(config, runner=runner)
        if instance.enabled:
            instances.append(instance)
    return instances


def get_load_errors():
    _ensure_loaded()
    return dict(_LOAD_ERRORS)


def _ensure_loaded():
    """确保所有插件模块已加载"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    
    # 自动导入同目录下的所有插件模块
    plugin_dir = os.path.dirname(__file__)
    for filename in sorted(os.listdir(plugin_dir)):
        if filename.endswith('.py') and filename not in ('__init__.py', 'base.py'):
            module_name = filename[:-3]
            try:
                importlib.import_module(f'.{module_name}', package=__name__)
            except Exception as e:
                _LOAD_ERRORS[module_name] = str(e)
                print(f"加载插件 {module_name} 失败: {e}")
