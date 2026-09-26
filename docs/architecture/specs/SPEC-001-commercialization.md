# SPEC-001 Relay 商业化契约

版本 0.1；对应 PRD-001。目录参照 typeleast 的实际职责组织，旧 code/ 保留桌面源码以避免破坏运行入口，新 backend/ 承载商业 API 与官网，scripts/ 承载可复现命令，docs/ 按产品/架构/设计/工程/质量/安全组织。

## 运行边界

Python 3.12 + Django 5.2 LTS，开发 SQLite，生产 PostgreSQL；官网用 Django 模板与同域原生 CSS/JS。复用同属用户的 AppSwitcher 商业服务实现作为工程起点，独立数据库、签名密钥、环境变量与产品身份，不复用账户/秘密/运营配置。新的代码放 backend/，不改参考项目。

账号、支付、订单、退款、客服和签名协议沿用其经过本地验证的模块边界并重新测试。Relay 的产品参数：产品名 Relay，环境 RELAY_ENV / RELAY_RUNTIME_DIR，aud=relay，terms/privacy 2026-09-26，PUBLIC_URL 默认 http://127.0.0.1:8016，minimumOS=12.0（开发目标，不等于已完成各系统版本验收），architecture=arm64（本机开发制品）。

## HTTP

camelCase JSON；时间 UTC Unix 整秒或 null；价格整数分。错误为非 2xx + `{error:{code,message}}`。网页使用 HttpOnly 会话 Cookie+CSRF；桌面使用 Bearer。生产仅 HTTPS，开发只允许 loopback HTTP。端点不得返回 OTP 或签名私钥。

- GET `/api/v1/config`：`{productName,environment,plans:[{id,name,months,amount,currency}],payments:{wechat,alipay,simulated},download:{available,url,version,minimumOS,architecture},supportEmail,termsVersion,privacyVersion,freeFeatures,proFeatures}`。plans 只有 monthly=1200/1月、yearly=9800/12月。未验证的分发制品 available=false。
- POST `/api/v1/auth/request-code` `{email}`，POST `/api/v1/auth/verify-code` `{email,code,acceptedTerms,termsVersion,privacyVersion}`；返回 me。统一注册/登录，不泄露账号是否存在。
- GET `/api/v1/me` → `{account:{id,email},entitlement:{status,trialEndsAt,paidUntil,validUntil,tier,features},license}`。原始 status=eligible/trial/paid/expired；tier=free/pro；未登录和无权益始终允许本地 Free。
- POST `/api/v1/trial/start`、POST `/api/v1/auth/logout`；GET `/api/v1/sessions` 与 POST `/api/v1/sessions/{id}/revoke`。
- POST `/api/v1/desktop/start` `{codeChallenge,state,deviceName}` → `{requestId,authorizeUrl,expiresIn:300,pollInterval:3}`。
- GET `/api/v1/desktop/request?request=...`（已登录浏览器）→ `{deviceName,expiresAt}`；POST `/api/v1/desktop/approve` `{request}`（session+CSRF）→ `{approved:true}`。浏览器确认不带 token、不跳任意 URL。
- POST `/api/v1/desktop/poll` `{requestId,codeVerifier,state}` → 未批准时 HTTP 202 `{status:"pending"}`，批准后一次性返回 `{accessToken,refreshToken,expiresIn:3600,sessionId,account}`。校验 S256/state、5分钟有效期、有限错误尝试，限制轮询频率；成功消耗授权。不可用 request UUID 单独取得凭据。
- POST `/api/v1/desktop/refresh` `{refreshToken}` → 轮转 token；旧 token 重放撤销会话。POST `/api/v1/desktop/logout`（bearer）。
- GET `/api/v1/orders/quote?planId=...`；POST `/api/v1/orders` `{planId,channel,idempotencyKey,acceptedTerms,termsVersion,expectedAmount,expectedCurrency}`。服务端按套餐定价并核对报价，变更返回409。
- GET `/api/v1/orders`、GET `/api/v1/orders/{id}`、POST `/api/v1/orders/{id}/sync`、POST `/api/v1/orders/{id}/refund` `{reason}`。仅 development 且明确开启允许 `/simulate`。
- GET/POST `/api/v1/support`、POST `/api/v1/support/{id}/messages`；GET `/api/v1/releases/latest`；GET `/healthz`、`/readyz`。

## 签名权益与桌面接入

Ed25519 JWS 固定 header `{alg:"EdDSA",typ:"JWT",kid:"license-v1"}`；payload `{iss,aud:"relay",sub,sid,iat,nbf,exp,entitlementUntil,status:"trial"|"paid",features:["history","compare","export_bundle"]}`。exp 为权益截止与签发+7天的较早值。校验固定发行者/随包公钥、算法、kid、签名、账号/会话和时间；不接受服务器响应里的替换公钥。回拨时钟不得延长离线权益。撤权不能立即影响完全离线的已签名授权，最多7天是明确边界。

`DetectionEngine` 不导入商业模块。独立 `relay/commercial/` 承载账户客户端、Keychain、license verifier、历史存储与导出。历史本地留存30天，有限记录总量；默认脱敏地址/SSID/URL敏感信息；HTML escape，导出失败不丢原记录。Basic 报告始终免费，Pro 失效保留历史且不新增付费操作。

## 可靠性契约

核心命令用 argv、返回 stdout/stderr/returncode/timeout；检测失败为 unknown/检查未完成，不得变成 ok。VPN 默认自动证据探测：有效接口+档案锚点路由一致，客户端进程不能单独证明接口归属，冲突显示未确认。新安装不注入公司 DNS、不默认关闭 IPv6。已有明确公司配置保留。

修复仅运行内建允许动作，不执行配置任意 shell；未知 VPN 不触发 DNS 切换。执行前重新检测、保存仅涉及字段的快照；检查返回码、目标状态及基线退化；失败回滚并明确区分回滚失败，不把所有 UI 返回行标成功。测试通过注入模拟命令进行，禁止在测试中修改真实网络。

## 页面与视觉

路由 `/`、`/features/`、`/pricing/`、`/download/`、`/login/`、`/account/`、`/orders/`、`/support/`、`/privacy/`、`/terms/`、`/desktop/authorize/`。保留 Relay 信号弧 logo；银黑中性色搭配克制的绿色诊断状态，官网语言中文。真实交互完整，未开放的支付/下载明确说明；不把本机内网信息放到公开截图。
