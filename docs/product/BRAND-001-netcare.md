# NetCare 产品命名

2026-09-27：用户确认以 **NetCare** 替代 Relay，并要求统一现有页面与功能中的名称。定位为网络健康保障 Agent。本次仅做品牌更名，不恢复已暂停的整体蓝图开发。

## 对外名称

- 产品、菜单栏、窗口标题、通知、关于/退出与移除提示：NetCare。
- 免费/商业能力：NetCare Free、NetCare Pro；现有权益、价格与条款内容不变。
- 官网首页、功能、价格、下载、账号、登录授权、支持、隐私/条款和后台管理：NetCare。
- 登录邮件发件人默认显示名、验证码主题、新支付请求说明：NetCare。不改运营方已配置的邮箱地址，不改历史订单。
- 新导出报告的标题与产品字段：NetCare；文件前缀 `netcare-report-`、`netcare-profile-`。既有导入 schema 不变。
- 新 Mac 构建：`NetCare.app`、主程序 `NetCare`、`NetCare-3.0.0-arm64-local.zip`。仍为本地开发包，未经正式签名公证，不代表上线。

## 保留的兼容性名称

以下是内部标识，不作为新品牌展示，也不在本次迁移：

- `com.wangxinlei.relay` 及现有 LaunchAgent、Mach service、helper 标识。
- 用户的 `Library/Application Support/Relay`、日志文件、旧 `.config/relay` 与 Windows 数据目录。
- `com.relay.account.*` 钥匙串项、现有授权 audience、Cookie、浏览器主题偏好键。
- `relay-*` 数据 schema、Python 包和入口模块、`RELAY_*` 环境变量。
- 原生类、协议、`RelayIPC.dylib`、`RelayHelper` 二进制与受签名的 helper 元数据键。
- 原有图标图形及资源文件名；这是名称统一，不是重新设计 Logo。

新主程序名称同步到用户服务的 BundleProgram、ProgramArguments 以及签名配对路径。不能只改 App 外壳而遗留旧可执行路径。

旧构建、历史报告、历史 QA 记录和归档中出现 Relay 属于历史证据，不追溯改写；用户既有资料不删除或重新初始化。当前 README、产品说明及 review 文档采用新名称。

## 验证范围

本次回归覆盖品牌显示、报告导出、账号服务、原生最小窗口、启动入口及兼容性标识。网页预览使用隔离临时数据库和本地服务，不发送真实邮件、不发起支付或部署。原生测试使用 FakeMac，不修改宿主网络。

暂停前已有的断线恢复测试失败及匿名 XPC 超时事项不在此次更名中修复，不把品牌回归通过写成完整产品已验收。

### 本次验证结果

- 更名相关桌面/核心/导出/生命周期/原生通信回归 253 项全部通过，69.479 秒。第一轮发现一个测试仍查找旧 `relay-report-` 文件名前缀，改为 `netcare-report-` 后重跑通过，没有改变导出权限逻辑。
- 商业后端 80 项：75 通过、5 项跳过，10.369 秒；包含公共页面、产品配置与登录邮件品牌断言。独立环境使用现有锁文件 `uv sync --frozen`；`--locked` 检测到既有清单/锁文件一致性问题，本次不更新依赖锁。
- Playwright 使用独立 Chrome 实例检查 11 条页面路径 × 桌面/手机两种尺寸，共 22 次；未登录的受保护路径正常跳转登录页。页面标题与正文品牌、无横向溢出和无 JS 未捕获异常检查通过。临时服务和浏览器已退出，没有登录真实账号。
- Cocoa 五页最小窗口深浅色截图已刷新；新品牌文字宽度及窗口标题断言通过。查看 `build/ui-preview/workspace-overview-light.png`、`workspace-overview-dark.png`；网页证据为 `netcare-web-home-mobile.png` 等。
- 关键静态检查、脚本语法和差异空白检查通过。机械网页检测无发现，但无法解析 Django 模板中的 stylesheet 链接，因此以浏览器实渲染作为补充，不声称该检测覆盖全部样式。
- `scripts/build-macos.sh --self-check` 通过，冻结自检报告 `product=NetCare`、`ok=true`；App 显示名与可执行名均为 NetCare，Bundle ID 不变。ZIP 解压严格验签、85 项构建输入哈希匹配通过。
- 新 NetCare 冻结包的隔离空闲生命周期测试通过：独立核心启动、重连、正常停机和资料保留；`guard_enabled=false`、`check_started=false`、`service_registered=false`。

开发包：`dist/macos/NetCare-3.0.0-arm64-local.zip`。SHA-256：`d2003a55ec0b09147af66fa60ee968dfc1b2cfa793d0ebaa97231f0818f92045`。未替换已安装的旧应用、注册系统服务或部署官网。这个包包含当前工作区已有的在途恢复代码，未声称已修复暂停前的失败用例。
