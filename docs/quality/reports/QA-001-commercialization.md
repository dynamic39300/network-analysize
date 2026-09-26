# QA-001 Relay 3.0 商业化本地交付

2026-09-26 · 本地候选版。目标是把账号、官网、收费方案和桌面权益做成可运行、可复核的完整链路。已完成这一范围；**尚未公开部署、开启真实收款或发布公证安装包**。

## 交付范围

- 官网：完整首页、功能、价格、下载、邮箱登录、账号/设备、订单、支持、隐私、条款和桌面授权页，支持深浅色及手机布局。开发模式、未开放购买和未发布下载均明确显示。
- 账号：文件邮件开发环境 / SMTP 生产环境，限流验证码、条款版本、Cookie/CSRF、设备 PKCE 轮询、Keychain 凭证、可撤销及轮换会话。管理员密码与公共邮箱认证分离；角色提升后旧邮箱/桌面会话失效。
- 收费：Free 独立离线使用；Pro 价格提案月度 ¥12 / 年度 ¥98，明确开启的 14 天试用，按账号管理本人多台 Mac。服务端定价、幂等订单与权益账本、微信/支付宝适配、查单补偿、运营退款与支持后台。
- 桌面：独立 arm64 应用构建；30 天本地历史、快照对比、脱敏导出和最多 7 天签名权益；账号/历史/诊断工作线程隔离。未知状态不报健康；修复先保存基线、确认、复核、验证与必要时回退。
- 工程：两套 uv 锁、单命令本地启动、真实 HTTP 联测、临时 PostgreSQL 验证、部署/签名脚本、产品/协议/设计/安全文档。现有安装副本与系统网络未被本轮验证修改。

## 实测结果

环境：本机 Apple Silicon、Python 3.12；临时 PostgreSQL 16.2（pgserver 0.1.4）。

| 检查 | 结果 | 能证明什么 |
|---|---|---|
| 桌面 unittest | 74 / 74 通过 | 45 诊断/修复、13 商业授权/历史、16 菜单并发/兜底 |
| 后端 PostgreSQL | 79 / 79 通过，0 跳过 | 包括 5 个真实数据库行锁并发测试、4 个管理员来源隔离回归 |
| 真实 loopback HTTP 联测 | 通过 | 文件验证码/CSRF、试用、浏览器批准与桌面 PKCE、实际后端签名由实际客户端验证、服务端报价、幂等模拟付款、工单/退款申请、退出保留 Free |
| Django / ruff / migration drift | 通过 | 后端系统检查无问题、格式与静态检查通过、迁移无遗漏 |
| 脚本 / JS / diff 检查 | 通过 | 本地脚本语法、官网 JS 解析及差异空白检查 |
| 依赖审计 | 两环境未报告已知漏洞 | 仅代表扫描时依赖库数据库结果，不等于应用安全认证 |
| 浏览器验收 | 通过 | 隔离前端流程共 78 次 API 请求；真实服务 8 个公开页加载、CSP、响应式、主题与无未捕获 JS 错误 |
| Lighthouse 13.5.0 | P 79 / A 100 / BP 96 / SEO 100 | 本地开发首页一次测量，不代表生产或所有页面 |
| 部署模板 | Shell / YAML / 配置路径审查通过 | 本机没有 Docker，未构建或启动生产容器 |

Lighthouse 性能的主要余量是约 905 KiB 品牌图和开发服务器的压缩/缓存；Best Practices 包含匿名 `/me` 返回 401 的控制台提示。正文与控件对比度检查均达到 4.5:1。没有把开发服务器分数包装成生产指标。

macOS 构建输出为 `dist/macos/Relay.app`、`Relay-3.0.0-arm64-local.zip` 和 `build-metadata.json`；随包包含 Python、Cocoa、Keychain 后端、cryptography 和 7 个检查插件。构建脚本执行 arm64 校验、ad-hoc 签名验证与无 GUI self-check；self-check 实际执行 Ed25519 签验及依赖导入。最终构建结果见同目录 [构建元数据](evidence/macos-build-metadata-20260926.json)。构建改在独立临时目录完成，归档后解压副本严格验签通过；稳定 ZIP 为 18,593,691 字节，SHA-256 `968928dd8c6b3c259cfc5e1ee170479e8a1b692b27b42adf693e81dd881f0c40`。目录中的 App 可能随后被 Finder/系统补充元数据，应以经过复验的 ZIP 为交付依据。本地 ad-hoc 包不是 Developer ID 公证包。

## 复现与证据

仓库根目录：

```sh
./scripts/dev.sh web
# 另一个终端可执行 ./scripts/dev.sh desktop；源码桌面实际联网诊断只读，修复需界面确认。
uv run --directory apps/macos python -m unittest discover -s ../../tests -v
uv run --directory backend --with pgserver python ../scripts/verify_postgres.py
uv run --directory backend python ../scripts/verify_commercial_flow.py
./scripts/build-macos.sh --self-check
```

系统写命令在单元测试中全部模拟；HTTP 联测使用私有临时数据库与签名密钥、合成邮箱、文件邮件及模拟付款，退出后删除临时状态。PostgreSQL 只监听私有目录 Unix socket，测试结束自动停止删除，没有连接现有数据库。登录、付款、退款的生产适配测试使用合成数据与签名，不产生真实邮件或交易。

- [Lighthouse 原始 JSON](evidence/lighthouse-20260926-home.json)
- [后端依赖扫描](evidence/backend-dependencies.json)、[桌面依赖扫描](evidence/desktop-dependencies.json)
- [官网浅色](evidence/website-light.png)、[官网深色](evidence/website-dark.png)、[手机价格页](evidence/pricing-mobile.png)
- [安全与数据模型](../../security/SEC-001-data-and-threat-model.md)、[官网交互记录](../../design/DESIGN-001-relay-website.md)

## 正式上线尚需完成

| 所需输入/验证 | 当前状态 |
|---|---|
| 域名、经营主体、客服邮箱、条款及退款政策审阅 | 待确定；页面为审阅稿，没有伪造主体或合规结论 |
| SMTP 与真实验证码到达率、退信处理 | 仅文件邮件闭环已验证 |
| 微信/支付宝独立商户、密钥与受控实际交易 | 适配已实现；真实付款/回调重试/退款/对账未验，默认关闭 |
| 生产 PostgreSQL TLS、备份恢复与容器卷权限 | 本地数据库并发已验；生产环境未验 |
| 生产 HTTPS 反向代理、监测告警和公网运行 | 提供部署模板，未创建或发布服务器 |
| Apple Developer ID、公证、Gatekeeper、第二台 Mac | 提供签名脚本；没有调用真实签名身份或上传公证 |
| 最低 macOS 与实际菜单/Keychain 用户体验矩阵 | 元数据目标 macOS 12 / arm64；完整设备矩阵未验 |
| 图片传输与生产静态缓存、完整键盘/读屏巡检 | 基础可访问性已验；仍需发布前整体复核 |

离线撤销最多延迟 7 天是设计边界；Pro 到期不主动删除旧历史行，30 天是可见查询窗及写入时清理窗口。原始修复快照仅存本机以支持恢复，并非分享用的脱敏报告。详见安全文档。

发布手册：[服务部署与恢复](../../../deploy/commercial/README.md)、[桌面构建与签名](../../../apps/macos/README.md)。正式上线前先在独立预发布环境验证，不将本地通过等同于完成商业运营。
