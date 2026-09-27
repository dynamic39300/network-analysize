# NetCare 商业服务

独立的 NetCare 官网与商业 API，Python 3.12 / Django 5.2 LTS。开发 SQLite；生产 PostgreSQL。账号、签名密钥、数据库、邮件和支付运营配置与参考产品独立。接口基线见 [SPEC-001](../docs/architecture/specs/SPEC-001-commercialization.md)。

这里的自动化结果验证本地协议与模拟账本闭环。**本地模拟不等于真实商户验证、真实邮件投递、生产 PostgreSQL 并发验收或 Apple 签名/公证验收。** 未配置的真实支付默认关闭，未经核验的下载不会公开为可用。

## 从仓库根目录启动

```sh
uv sync --directory backend --locked
uv run --directory backend python manage.py migrate
uv run --directory backend python manage.py init_development
uv run --directory backend python manage.py runserver 127.0.0.1:8016
```

访问 `http://127.0.0.1:8016/`。默认独立运行目录为 `backend/.runtime/`：数据库、随机 Django secret、开发签名密钥和邮件均在此处，已被 Git 忽略。文件权限属于当前操作系统账户，不应复制到公开制品。运行目录可用绝对路径 `RELAY_RUNTIME_DIR` 指定。`.env.example` 仅是环境变量示例，程序不会自动读取 `.env`。

默认邮件后端只将验证码写入 `.runtime/mail/`，不会发往真实邮箱；验证码不在 API、普通日志或页面输出。开发者可在自己机器检查该目录完成合成邮箱的登录。`init_development` 不会创建账号、管理员、试用权益或支付记录，也不会输出私钥。

模拟付款只有 `RELAY_ENV=development` 且显式设置 `SIMULATED_PAYMENTS=1` 才可用。要演示模拟闭环，请在启动进程前设置该变量；每笔模拟订单始终标记 `channel=simulated`，真实渠道订单无法使用模拟付款端点。生产环境拒绝携带模拟支付开关启动。

## 账号与桌面授权

- 邮箱验证码 10 分钟有效，一次消费，最多 5 次验证失败；发送按邮箱分钟/小时及可信来源 IP 限流。投递抛错或返回未发送时立即失效，不建立登录。
- 登录/注册统一入口，必须明确同意当前 `termsVersion` / `privacyVersion`（2026-09-26）。浏览器使用 HttpOnly、SameSite=Lax Cookie 与 CSRF；管理员账号不可通过公共邮箱验证码登录，后台使用独立的密码认证入口。邮箱认证与管理员密码认证使用不同 backend；账号提升为管理员后，原邮箱 Cookie / desktop bearer / refresh 均失效。旧混合认证 backend 的 Cookie 需要重新登录。
- 桌面先生成 43–128 字符随机 PKCE verifier，以 SHA-256 base64url 无填充生成 `codeChallenge`，再生成 32–256 字符随机 `state`。`POST /api/v1/desktop/start` 返回 `requestId`、`authorizeUrl`、`expiresIn=300` 与 `pollInterval=3`。
- 浏览器登录后查看设备名并确认。`POST /api/v1/desktop/approve` 使用会话与 CSRF，只返回 `{approved:true}`；不包含回调 URL、access token 或 refresh token。授权一旦绑定账户，其他账户不能覆盖。
- 桌面每 3 秒 `POST /api/v1/desktop/poll`，JSON 为 `{requestId,codeVerifier,state}`。正确但尚未批准返回 HTTP 202 `{status:"pending"}`；批准后一次返回 access/refresh token、sessionId 和已批准账户，随后重放返回 401。requestId 本身不能换取 token。
- 授权有效期 5 分钟；每个请求最多 5 次错误 proof；合法轮询间隔至少 3 秒，过快返回 429，另有 IP 上限 600 次/小时。合法 pending 不消耗错误次数。并发通过数据库行锁保证只生成一个会话。
- 已移除参考产品的 `desktop/exchange` 及自定义 URL 回调流程。`desktop_api_fixture` 产生新的 requestId/verifier/state 格式，仅支持 loopback 开发环境的合成账号；生成文件属于短期凭证，权限 0600，不得提交或分享。
- access token 有效 1 小时；refresh token 有效 90 天并每次轮换，旧 refresh token 重放会撤销对应会话。会话撤销、登出、停用账号后不能在线获取新权益。

## Free、试用与离线签名

Free 不需要登录、服务器或付款。服务器 config 的 `freeFeatures` 为 `basic_diagnostics`、`menubar_monitoring`、`basic_report`、`safe_repairs`；`proFeatures` 为 `history`、`compare`、`export_bundle`。`/me` 的 `tier=free` 时 `features=[]` 表示没有额外 Pro 权益，客户端基本功能应始终可用。

试用须用户明确开启，每个账号一次，连续 **14 × 24 小时**，不按设备重置、不自动扣款。已有购买记录的账号不能再次领取试用。购买会保留尚未结束的试用与已购剩余时间，按上海时区自然月追加。服务器只提供月度 ¥12（1200 分）与年度 ¥98（9800 分）套餐；请求中客户端的金额仅用于核对当前报价，不能改变服务端金额。

离线 Pro JWS 使用 Ed25519，header 固定 `alg=EdDSA, typ=JWT, kid=license-v1`；`aud=relay`，`iss=PUBLIC_URL`，绑定账户 `sub` 和桌面会话 `sid`。features 固定为上述三个 Pro 项。`exp=min(entitlementUntil,iat+7×86400)`，试用或付费到期后不签发新 Pro license。网页会话没有离线 license。

桌面必须信任随应用发布的固定公钥与发行者，不能使用 API 返回的替代公钥。完全离线的已签名授权无法即时撤销，最长 7 天是协议限制；本地时钟回拨防护由桌面实现。`license_fixture` 可导出带临时测试公钥的公开签名向量用于跨语言测试，不能把测试公钥作为生产信任根。

## 订单与支付

所有订单/退款/工单/设备会话按账号隔离。幂等键作用域为账号，重复支付通知只发一份权益。浏览器支付回跳不作为到账证据。微信 Native API v3 验证响应/通知签名与公钥 ID，解密通知后核对 appid/mchid、订单、金额、币种、交易状态；支付宝 RSA2 验证签名及 appid/seller、订单和精确金额。失败一律不发放权益。

退款通过后台具有 `commerce.process_refund` 权限的运营者提交；用户申请不会立即撤权。支付渠道核验成功才撤销对应 grant，并保留其他订单的权益。账本与审计记录不得因部署回退而删除。

支付适配器仍需依据商户接入时的官方文档，完成真实商户沙箱/受控小额付款、重复通知、退款、查单及密钥轮换演练后才能对外开放。

## 生产配置与运维

`RELAY_ENV=production` 时启动会检查：

- 代码目录外的绝对 `RELAY_RUNTIME_DIR`、至少 50 字符 `DJANGO_SECRET_KEY`、HTTPS origin 格式的 `PUBLIC_URL`。
- PostgreSQL `DATABASE_URL` 与 `DATABASE_SSLMODE=verify-full`；不得使用 SQLite 或放宽数据库 TLS。
- SMTP 后端、`EMAIL_HOST` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD`，`EMAIL_USE_TLS=1`，有效非 localhost 发件标识，以及 `SUPPORT_EMAIL` / `LEGAL_ENTITY_NAME`。
- `LICENSE_PRIVATE_KEY_FILE` 指向可解析 Ed25519 私钥；缺失/格式错误不启动。签名密钥应在独立秘密存储中管理并备份，配套公钥随桌面发行。
- 禁止模拟支付与开发邮件后端。真实支付只有显式 `WECHAT_ENABLED=1` 或 `ALIPAY_ENABLED=1` 才启用，并要求完整商户配置、可读 RSA 密钥（至少 2048 位）、微信 32 字节 API v3 密钥。任何缺失配置都拒绝启动，不降级成假成功。

微信需要 `WECHAT_APP_ID/MCH_ID/CERT_SERIAL/PRIVATE_KEY_FILE/PUBLIC_KEY_ID/PUBLIC_KEY_FILE/API_V3_KEY`。支付宝需要 `ALIPAY_APP_ID/SELLER_ID/PRIVATE_KEY_FILE/PUBLIC_KEY_FILE`。不要把值写入仓库、聊天、命令输出或测试报告。

HTTPS 反向代理必须覆盖转发头，且后端不可直接暴露；仅此条件满足时设置 `TRUST_PROXY_HTTPS=1`。限流 IP 只信任 `TRUSTED_PROXY_IPS` 中明确列出的最后一跳地址及其单值 `X-Real-IP`，默认忽略外部代理头。静态文件以 `collectstatic --noinput` 生成到 `RELAY_RUNTIME_DIR/staticfiles`，应用进程可用 `gunicorn config.wsgi:application --bind 127.0.0.1:8016` 从 backend 工作目录启动；代理、进程管理与环境值由部署配置提供。

定时执行 `python manage.py reconcile_payments` 对待付款/退款查单，`python manage.py purge_expired_auth` 清理过期认证工件。`/healthz` 是进程存活探针，`/readyz` 检查数据库以及生产签名密钥；它们不声称邮件和商户端到端可用。

升级前备份数据库与签名密钥，先执行迁移；本轮 `0004_relay_device_polling` 去掉旧一次性交换码字段，加入轮询时间与最低 OS 元数据。旧 AppSwitcher callback 客户端不兼容新的 NetCare API。回退必须同时协调 API/客户端版本；数据库中的已完成交易与权益不可回删。生产环境先在独立预发布数据库验证迁移与恢复。

## 验证

```sh
uv sync --directory backend --locked
uv run --directory backend ruff check .
uv run --directory backend ruff format --check .
uv run --directory backend python manage.py check
uv run --directory backend python manage.py makemigrations --check --dry-run
uv run --directory backend python manage.py test commerce.tests
```

测试覆盖邮件失效与限制、条款、PKCE pending/频率/state/错 verifier/跨账户/消费重放、refresh 重放撤销、14 天试用、7 天 JWS、账号隔离、服务端价格、支付通知签名及幂等、退款、补偿查单和生产 fail-closed。生产配置测试只使用临时合成密钥与虚构 SMTP/商户信息，不发真实请求。

SQLite 会跳过需要 `select_for_update` 的 PostgreSQL 并发用例。可从仓库根目录运行独立的临时 PostgreSQL 验证：

```sh
uv run --directory backend --with pgserver python ../scripts/verify_postgres.py
```

此脚本只允许普通 macOS/Linux 用户运行，在私有临时目录初始化新数据库，通过 Unix socket 连接并断言没有 TCP 监听；清除继承的数据库/商户/邮件配置，使用仅此测试进程内的数据库地址覆盖。不会连接已有数据库，不修改生产 TLS 规则，不把 pgserver 加入生产锁文件；结束自动停止服务器并删除临时数据。

2026-09-26 实测环境：pgserver 0.1.4 / PostgreSQL 16.2，全部 79 项 Django 测试通过，5 项并发用例实际执行，0 跳过。该结果验证真实 PostgreSQL 上的行锁行为；生产 PostgreSQL 的 TLS、备份/恢复和部署权限仍须在预发布环境验收。端到端邮件/支付及正式签名下载仍是上线前独立验收项。
