# Relay · Network 诊断

macOS 菜单栏网络诊断工具。3.0 商业化本地候选版包含独立桌面应用、官网、邮箱账号、设备授权、试用、订单与支付适配。项目目录参照 `202609-typeleast` 的产品、架构、实施与验收职责划分；沿用 Relay 的银黑信号标识。

**当前状态：可本地运行与验证，尚未公开发布或开通真实收款。** 当前机器正在运行的旧安装副本不会被构建脚本替换；商业账号、密钥和数据与参考项目独立。

## 本地预览

需要 `uv`（锁定 Python 3.12 依赖；桌面需要 Apple Silicon Mac）。从本项目根目录执行：

```sh
./scripts/dev.sh web
# 官网：http://127.0.0.1:8016/
```

开发验证码写入 `backend/.runtime/mail/`，不会发送真实邮件；可使用合成邮箱完成登录。真实支付及模拟付款默认均关闭。浏览器开启 14 天试用后，可在另一个终端启动源码桌面预览：

```sh
./scripts/dev.sh desktop
```

此命令为源码应用生成只含本地服务地址与公钥的配置，登录凭证仍保存到 macOS Keychain。会出现独立的 Relay 图标，请避免与旧版同时执行网络修复。源码预览的数据在 `~/Library/Application Support/Relay/`；新目录首次建立时只复制旧配置，不覆盖原文件。`Ctrl+C` 结束前台官网服务；菜单“退出”结束源码应用。

## 产品与收费提案

| 能力 | Relay Free | Relay Pro |
|---|---|---|
| 基础检测、菜单栏监测、脱敏基础报告、确认后的安全修复 | 永久免费，离线可用 | 包含 |
| 30 天本地历史、快照比较、HTML/JSON 诊断包 | — | 包含 |
| 初始定价 | ¥0 | ¥12/月或 ¥98/年 |
| 试用与设备 | 无需登录 | 每账号一次 14 天；本人多台 Mac |

首期采用主动购买续期，不自动扣款。到期恢复 Free。Pro 签名权益最多离线缓存 7 天；数据默认留在本机。官网的价格是可调整的首发提案，支付商户未就绪时购买按钮显示真实不可用状态。

## 项目结构

```text
apps/macos/                Python 依赖锁、PyInstaller 配置与打包说明
backend/                   Django 官网、商业 API、运营后台、迁移和测试
code/                      菜单栏入口与独立诊断核心；legacy shell 工具保留
code/relay/commercial/      Keychain 账号、签名权益、本地历史和脱敏导出
tests/                     桌面诊断、修复、账号和菜单并发回归
scripts/                   本地启动、协议联测、PostgreSQL 验证、构建和签名
deploy/commercial/         商业服务部署模板与发布/恢复说明
docs/product/prds/        产品、用户和价格提案
docs/architecture/specs/  协议与架构
docs/design/              官网设计及交互验收
docs/engineering/tickets/ 实施记录
docs/quality/reports/     实测证据与发布差距
docs/security/            数据与威胁模型
archive/                   历史版本
```

旧 `code/setup.py`、launchd plist 和 shell 安装脚本属于历史链路；3.0 使用 `scripts/build-macos.sh`。编号文档保留历史信息，当前交付以本 README 和 [文档导航](docs/README.md) 为准。

## 构建与验证

```sh
./scripts/build-macos.sh --self-check
# dist/macos/Relay.app 与 Relay-3.0.0-arm64-local.zip：本地 ad-hoc 签名，尚未公证

uv run --directory apps/macos python -m unittest discover -s ../../tests -v
uv run --directory backend python manage.py test commerce.tests
uv run --directory backend --with pgserver python ../scripts/verify_postgres.py
uv run --directory backend python ../scripts/verify_commercial_flow.py
```

默认打包支持 Free，不内置开发账号服务。配置正式服务的构建方式见 [桌面构建说明](apps/macos/README.md)。协议联测使用一次性的本地数据库、文件邮件和模拟付款，验证官网与真实桌面客户端的 PKCE、签名、试用、报价、幂等付款及退出链路，不产生真实交易。数据库并发测试在临时 PostgreSQL 上运行；系统网络写操作由测试桩代替。

详细接口、管理员、商户和数据维护见 [后端说明](backend/README.md)；交付与尚未验证的上线条件见 [验收报告](docs/quality/reports/QA-001-commercialization.md)。

## 正式上线所需输入

需要确定域名、经营主体和客服邮箱，并提供独立生产 SMTP、PostgreSQL/TLS、支付商户与 Apple Developer ID/公证配置。生产模式缺少必要配置会拒绝启动。真实商户通知/退款、邮件送达、备份恢复、签名安装和第二台 Mac 兼容性必须完成预发布验证；目前不会把开发安装包标成正式下载。
