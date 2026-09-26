# 商业服务部署审阅稿

这里提供 Docker 镜像、Compose 与 HTTPS 代理配置，不创建服务器、不上传密钥、不启动真实支付。后端配置契约见 [backend README](../../backend/README.md)。真实域名、SMTP、商户、证书与退款运营政策需要分别验收后才能公开上线。

## 运行边界

- Python 3.12、`backend/uv.lock` 的生产依赖、Gunicorn、Django / WhiteNoise。代码镜像只读；UID 10001 仅写 `/var/lib/relay` 与临时目录，`STATIC_ROOT=/var/lib/relay/staticfiles`。
- PostgreSQL 使用独立外部实例，不附带无 TLS 的演示数据库。强制 `verify-full`，挂载对应 CA 到 `/run/relay-secrets/postgres-ca.pem`。备份、恢复、容量与数据库迁移由运维管理。
- 启动首先运行生产配置检查；缺失 PostgreSQL、SMTP、经营主体、服务邮箱或可读 Ed25519 私钥即失败。迁移未应用时 Web 启动失败，不自动修改交易库结构。
- 微信与支付宝默认关闭，模拟支付强制关闭。显式开启渠道后任何商户密钥缺失都不能启动。该模板不声称完成真实付款、查单或退款验收。
- Nginx 与 Web 共享网络命名空间，Gunicorn 只绑定 `127.0.0.1:8016`；唯一可发布端口为 Nginx 的 443。代理覆盖协议与单值来源 IP，应用仅信任 `127.0.0.1`。不要另行暴露 8016 或移除代理头覆盖规则。
- Nginx 与 Gunicorn 的访问日志仅含路径，不记查询字符串、Cookie、Authorization 或正文。禁止开启敏感请求调试日志。

## 预发布步骤

1. 在仓库外创建生产配置文件和秘密目录，参考 `.env.example`。文件设为 0600，目录按运维权限管理；需要由容器 UID 10001 读取的私钥和 CA 应给予该 UID 只读权限。不要把实际值粘贴到聊天、Git、构建参数或日志。
2. 准备正式 HTTPS 证书目录：`fullchain.pem`、`privkey.pem`。初始映射只有 `127.0.0.1:8443`，如公开监听须明确设置 `RELAY_HTTPS_BIND=0.0.0.0:443` 并配置防火墙。`PUBLIC_URL` 应与用户实际访问的固定 origin 一致。示例 `.invalid` 地址不可上线。
3. 将 Python、uv 和 Nginx 镜像参数固定为审核过的 digest，并记录应用 commit、锁文件哈希与镜像 digest。当前默认 tag 方便审阅，不构成位级可复现的生产镜像声明。
4. 使用独立预发布数据库先验证迁移、邮件送达、HTTPS Cookie/CSRF、PKCE 与签名权益，再进行受控真实商户验证。不要将测试命令指向生产交易数据库。

从仓库根目录执行以下**人工部署步骤**；本次实现没有执行它们：

```sh
docker compose --env-file /srv/relay/config/relay.env -f deploy/commercial/compose.yaml config --quiet
docker compose --env-file /srv/relay/config/relay.env -f deploy/commercial/compose.yaml build --pull
# 数据库与签名密钥完成可恢复备份后，单独执行迁移。
docker compose --env-file /srv/relay/config/relay.env -f deploy/commercial/compose.yaml run --rm web python manage.py migrate
docker compose --env-file /srv/relay/config/relay.env -f deploy/commercial/compose.yaml up -d
```

不要直接打印 `compose config` 展开结果，其中可能包含环境秘密。使用 `config --quiet` 做校验。镜像只 COPY 后端代码、模板与静态目录，不 COPY `.runtime`、`.env` 或数据库。

## 运维与回退

Web 启动检查迁移状态并执行 `collectstatic --noinput`，随后启动 Gunicorn；`/healthz` 只表示进程存活，`/readyz` 检查数据库及签名密钥，不代表 SMTP 与商户服务畅通。容器探针使用合法 Host 和 HTTPS 代理标记从 loopback 调用，不放宽公网校验。

在已有受控调度系统配置以下命令，避免同一数据库多个任务重复高频运行；这里不自动安装 cron：

```sh
docker compose --env-file /srv/relay/config/relay.env -f deploy/commercial/compose.yaml exec -T web python manage.py reconcile_payments
docker compose --env-file /srv/relay/config/relay.env -f deploy/commercial/compose.yaml exec -T web python manage.py purge_expired_auth
```

每次升级前备份 PostgreSQL 与签名密钥，在预发布数据库验证恢复。保留上一版已验收镜像与兼容客户端，但不要为了回退删除交易账本、订单或授权 grant。不可逆迁移必须前向修复或在维护窗口协调恢复，不按旧镜像擅自回滚数据库。

Ed25519 公钥随客户端固定发布，轮换不能只替换服务端私钥；须协调客户端信任根与最长 7 天离线授权边界。商户密钥轮换、SMTP故障、回调重试、退款及账本对账需要独立演练。正式签名安装包验证完成后才可设置下载发布信息。

## 本次验证范围

本机没有 Docker 可执行程序，因此没有声称成功构建镜像或运行 Compose。模板经过 YAML 结构、Shell 语法及代码路径审查；真实容器、TLS 代理、持久卷权限、生产 PostgreSQL / SMTP / 商户需要在具备 Docker 与隔离凭证的环境中验证。
