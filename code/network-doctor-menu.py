#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Relay - 网络诊断与修复菜单栏工具 v2.0
插件化检测引擎 + 配置驱动修复引擎
"""

import rumps
import subprocess
import threading
import time
import os
import sys
from datetime import datetime
from PyObjCTools import AppHelper

# 确保脚本目录在 sys.path 中（用于导入 relay 包）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# 配置管理
try:
    from relay_config import ConfigManager
except ImportError:
    ConfigManager = None

# 插件化检测引擎与修复引擎
from relay.engine import DetectionEngine, FixEngine

# 设置进程名称
try:
    import setproctitle
    setproctitle.setproctitle("Relay")
except ImportError:
    pass

# ==================== 配置 ====================
APP_NAME = "Relay"

# 全局配置管理器
config = None
if ConfigManager:
    config = ConfigManager()
    config.load()
    CHECK_INTERVAL = config.get('general.check_interval', 30)
    LOG_FILE = os.path.expanduser(config.get('general.log_file', '~/Library/Logs/Relay.log'))
else:
    CHECK_INTERVAL = 30
    LOG_FILE = os.path.expanduser("~/Library/Logs/Relay.log")

MENUBAR_ICON = os.path.join(SCRIPT_DIR, "assets", "menubar", "relay-menubar-template.png")

# ==================== 工具函数 ====================

def log(msg):
    """写日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line)


def run_cmd(cmd, timeout=10):
    """执行 shell 命令"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception:
        return ""


# ==================== 菜单栏应用类 ====================

class NetworkDoctorApp(rumps.App):
    """Relay 菜单栏应用"""
    
    def __init__(self):
        super().__init__(APP_NAME, icon=MENUBAR_ICON, template=True, title=None)
        
        # 初始化检测引擎和修复引擎
        self.detection_engine = DetectionEngine(config)
        self.fix_engine = FixEngine(config, self.detection_engine)
        self.checking = False
        
        # 构建菜单
        self._build_menu()
        
        # 启动时立即检测一次
        self.run_check()
        
        # 启动后台定时检测线程
        self.background_thread = threading.Thread(target=self._background_loop, daemon=True)
        self.background_thread.start()
        
        log("菜单栏应用启动（插件化引擎）")
    
    def _build_menu(self):
        """构建初始菜单"""
        self.menu = [
            rumps.MenuItem("状态检测中...", callback=None),
            None,
            rumps.MenuItem(f"🔍 一键检测", callback=self.on_manual_check),
            rumps.MenuItem(f"🔧 一键修复", callback=self.on_fix),
            None,
            rumps.MenuItem(f"📋 查看详细报告", callback=self.on_show_report),
            None,
            rumps.MenuItem(f"⚙️ 设置"),
            rumps.MenuItem(f"ℹ️ 关于", callback=self.on_about),
            None,
            rumps.MenuItem(f"🚪 退出 Relay", callback=self.on_quit),
        ]
        
        # 设置子菜单
        settings_menu = self.menu["⚙️ 设置"]
        settings_menu.update([
            rumps.MenuItem("🏢 公司环境预设", callback=self.on_preset_company),
            rumps.MenuItem("🏠 个人代理预设", callback=self.on_preset_personal),
            rumps.MenuItem("📡 极简监测预设", callback=self.on_preset_minimal),
            None,
            rumps.MenuItem("🔄 重新探测环境", callback=self.on_redetect),
            rumps.MenuItem("📂 打开配置文件", callback=self.on_open_config),
        ])
    
    def _background_loop(self):
        """后台定时检测循环"""
        while True:
            time.sleep(CHECK_INTERVAL)
            self.run_check()
    
    def run_check(self):
        """执行检测并更新 UI"""
        if self.checking:
            return
        self.checking = True
        
        def _do_check():
            try:
                self.detection_engine.run_all()
                log(f"检测完成，发现 {len(self.detection_engine.issues)} 个问题")
                AppHelper.callAfter(self._update_ui)
            except Exception as e:
                log(f"检测线程异常: {e}")
            finally:
                self.checking = False
        
        threading.Thread(target=_do_check, daemon=True).start()
    
    def _update_ui(self):
        """更新菜单栏图标和菜单"""
        # 重建菜单
        self.menu.clear()
        
        # 状态摘要
        summary_lines = self.detection_engine.get_status_summary()
        for line in summary_lines:
            if line == "":
                self.menu.add(None)
            else:
                self.menu.add(rumps.MenuItem(line, callback=None))
        
        self.menu.add(None)
        
        # 操作项
        self.menu.add(rumps.MenuItem("🔍 一键检测", callback=self.on_manual_check))
        self.menu.add(rumps.MenuItem("🔧 一键修复", callback=self.on_fix))
        self.menu.add(None)
        self.menu.add(rumps.MenuItem("📋 查看详细报告", callback=self.on_show_report))
        self.menu.add(None)
        
        # 设置子菜单（重建）
        settings_item = rumps.MenuItem("⚙️ 设置")
        settings_item.update([
            rumps.MenuItem("🏢 公司环境预设", callback=self.on_preset_company),
            rumps.MenuItem("🏠 个人代理预设", callback=self.on_preset_personal),
            rumps.MenuItem("📡 极简监测预设", callback=self.on_preset_minimal),
            None,
            rumps.MenuItem("🔄 重新探测环境", callback=self.on_redetect),
            rumps.MenuItem("📂 打开配置文件", callback=self.on_open_config),
        ])
        self.menu.add(settings_item)
        
        self.menu.add(rumps.MenuItem("ℹ️ 关于", callback=self.on_about))
        self.menu.add(None)
        self.menu.add(rumps.MenuItem("🚪 退出 Relay", callback=self.on_quit))
    
    @rumps.clicked("🔍 一键检测")
    def on_manual_check(self, _):
        """手动触发检测"""
        try:
            self.run_check()
            for _ in range(30):
                if not self.checking:
                    break
                time.sleep(0.5)
            rumps.alert(
                title="检测完成",
                message=f"网络状态已更新\n\n发现 {len(self.detection_engine.issues)} 个问题",
                ok="好的"
            )
        except Exception as e:
            log(f"一键检测异常: {e}")
            rumps.alert(title="检测失败", message=f"错误: {str(e)[:200]}", ok="好的")
    
    @rumps.clicked("🔧 一键修复")
    def on_fix(self, _):
        """一键修复"""
        try:
            issues = self.detection_engine.issues
            if not issues:
                rumps.alert(
                    title="无需修复",
                    message="当前网络状态良好，未发现需要修复的问题",
                    ok="好的"
                )
                return

            planned_fixes = self.fix_engine.describe_fixes(issues)
            if planned_fixes == ["没有可安全自动修复的问题"]:
                rumps.alert(
                    title="无法自动修复",
                    message="检测到的问题当前没有可安全执行的自动修复动作，请查看详细报告。",
                    ok="好的"
                )
                return
            
            response = rumps.alert(
                title="一键修复",
                message=(
                    f"检测到 {len(issues)} 个问题，是否执行以下修复？\n\n"
                    + "\n".join(f"• {item}" for item in planned_fixes)
                    + "\n\n修复前会保存当前配置；修复后自检失败会自动回滚。"
                ),
                ok="修复",
                cancel="取消"
            )
            
            if response:
                fixed = self.fix_engine.fix_all(issues)
                fixed_text = "\n".join(f"✅ {f}" for f in fixed)
                rumps.alert(
                    title="修复完成",
                    message=f"已执行以下修复：\n{fixed_text}",
                    ok="好的"
                )
                self.run_check()
        except Exception as e:
            log(f"一键修复异常: {e}")
            rumps.alert(title="修复失败", message=f"错误: {str(e)[:200]}", ok="好的")
    
    @rumps.clicked("📋 查看详细报告")
    def on_show_report(self, _):
        """显示详细报告"""
        if not self.detection_engine.last_check:
            rumps.alert("暂无报告", "请先执行检测")
            return
        
        report = self.detection_engine.get_detailed_report()
        rumps.alert(
            title="网络状态详细报告",
            message=report[:3000],
            ok="关闭"
        )
    
    # ==================== 设置相关回调 ====================
    
    def _rebuild_engines(self):
        """配置变更后重建引擎"""
        self.detection_engine = DetectionEngine(config)
        self.fix_engine = FixEngine(config, self.detection_engine)
    
    def _apply_preset_and_restart(self, preset_name, _):
        """应用预设并重新检测"""
        if config:
            config.apply_preset(preset_name)
            config.save()
            self._rebuild_engines()
            self.run_check()
            rumps.alert(
                title="预设已切换",
                message=f"已切换到「{preset_name}」预设，配置已保存。",
                ok="好的"
            )
    
    def on_preset_company(self, sender):
        self._apply_preset_and_restart('company', sender)
    
    def on_preset_personal(self, sender):
        self._apply_preset_and_restart('personal_proxy', sender)
    
    def on_preset_minimal(self, sender):
        self._apply_preset_and_restart('minimal', sender)
    
    def on_redetect(self, _):
        """重新探测环境"""
        if config:
            detected = config.auto_detect()
            config.save()
            self._rebuild_engines()
            self.run_check()
            vpn_clients = ', '.join(detected.get('vpn', {}).get('clients', [])) or '无'
            proxy_clients = ', '.join(detected.get('proxy', {}).get('clients', [])) or '无'
            preset = detected.get('preset', 'unknown')
            rumps.alert(
                title="环境探测完成",
                message=(
                    f"Wi-Fi: {detected.get('wifi', {}).get('interface', '?')} / {detected.get('wifi', {}).get('service_name', '?')}\n"
                    f"VPN: {vpn_clients}\n"
                    f"代理: {proxy_clients}\n"
                    f"自动选择预设: {preset}"
                ),
                ok="好的"
            )
    
    def on_open_config(self, _):
        """打开配置文件"""
        if config:
            run_cmd(f"open -R '{config.config_path}'")
        else:
            run_cmd("open -R '~/.config/relay/config.json'")
    
    @rumps.clicked("ℹ️ 关于")
    def on_about(self, _):
        """关于"""
        preset_name = config.get('general.preset', 'auto') if config else 'unknown'
        plugin_count = len(self.detection_engine.checks)
        rumps.alert(
            title=f"{APP_NAME} v2.0",
            message=(
                "网络诊断与修复工具 - 插件化架构\n\n"
                f"已加载检测插件: {plugin_count} 个\n"
                "• Wi-Fi / VPN / 代理工具 / DNS\n"
                "• IPv6 / 系统代理 / 连通性\n\n"
                "• 自动环境探测 + 3 套预设\n"
                "• 配置驱动的修复引擎\n"
                f"• 后台每 {CHECK_INTERVAL} 秒自动刷新\n"
                f"• 当前预设: {preset_name}\n\n"
                f"日志: {LOG_FILE}\n"
                f"配置: ~/.config/relay/config.json"
            ),
            ok="关闭"
        )
    
    @rumps.clicked("🚪 退出 Relay")
    def on_quit(self, _):
        """退出应用"""
        try:
            response = rumps.alert(
                title="退出 Relay",
                message="确定要退出 Relay吗？\n\n注意：开机自启仍然启用，下次开机会自动启动。\n如需关闭开机自启，请在终端执行：\nlaunchctl unload ~/Library/LaunchAgents/com.wangxinlei.networkdoctor.plist",
                ok="退出",
                cancel="取消"
            )
            if response:
                log("用户主动退出应用")
                rumps.quit_application()
        except Exception as e:
            log(f"退出异常: {e}")
            rumps.quit_application()


# ==================== 主函数 ====================

def main():
    log("=" * 50)
    log(f"{APP_NAME} v2.0 启动（插件化引擎）")
    log(f"检测间隔: {CHECK_INTERVAL}秒")
    log(f"日志文件: {LOG_FILE}")
    
    app = NetworkDoctorApp()
    app.run()


if __name__ == "__main__":
    main()
