#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Relay 检测插件基类
所有检测插件必须继承此类并实现 check() 方法
"""

from abc import ABC, abstractmethod

# 插件注册表（模块级，供 register 装饰器使用）
_REGISTRY = {}


def register(check_class):
    """装饰器：注册检测插件"""
    if not issubclass(check_class, BaseCheck):
        raise TypeError(f"{check_class.__name__} 必须继承 BaseCheck")
    _REGISTRY[check_class.name] = check_class
    return check_class


def get_registry():
    """获取注册表"""
    return _REGISTRY


class BaseCheck(ABC):
    """检测插件抽象基类"""
    
    # 插件元数据（子类覆盖）
    name = "base"          # 插件唯一标识
    display_name = "基础检测"  # 显示名称
    description = ""       # 描述
    default_enabled = True # 默认是否启用
    
    def __init__(self, config=None):
        self.config = config
        self._enabled = self._load_enabled()
    
    def _load_enabled(self):
        """从配置加载启用状态"""
        if self.config:
            return self.config.get(f'{self.name}.enabled', self.default_enabled)
        return self.default_enabled
    
    @property
    def enabled(self):
        return self._enabled
    
    @enabled.setter
    def enabled(self, value):
        self._enabled = value
    
    def cfg(self, key, default=None):
        """安全获取配置，自动加插件名前缀"""
        if self.config:
            return self.config.get(f'{self.name}.{key}', default)
        return default
    
    def wifi_interface(self):
        """获取 Wi-Fi 接口名（公共工具）"""
        if self.config:
            iface = self.config.get('wifi.interface', 'auto')
            if iface and iface != 'auto':
                return iface
        return 'en0'
    
    def wifi_service(self):
        """获取 Wi-Fi 服务名（公共工具）"""
        if self.config:
            svc = self.config.get('wifi.service_name', 'auto')
            if svc and svc != 'auto':
                return svc
        return 'Wi-Fi'
    
    @abstractmethod
    def check(self, status):
        """
        执行检测，返回问题列表。
        
        Args:
            status: 共享状态字典，检测结果写入此处
            
        Returns:
            list of (severity, issue_type, description) 元组
            severity: 'high' / 'medium' / 'low'
            issue_type: 用于匹配修复规则的唯一标识
            description: 人类可读的问题描述
        """
        pass
    
    def get_status_lines(self, status):
        """
        返回该插件在状态栏摘要中显示的行。
        子类可覆盖以自定义显示。
        
        Args:
            status: 共享状态字典
            
        Returns:
            list of str，每行一个状态项
        """
        return []
