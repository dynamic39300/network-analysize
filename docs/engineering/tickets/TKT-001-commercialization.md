# TKT-001 商业化实施拆分

实施依据：本任务用户已要求开启目标模式实施，PRD-001/SPEC-001 是依该授权建立的可调整实施基线；不冒称另有逐文件审批。

| 任务 | 文件边界 | 验收与状态 |
|---|---|---|
| C01 商业 API | backend/ 除 templates/static | 邮箱认证、PKCE 设备轮询、权益、支付/退款幂等、账号隔离与生产限制测试；本地实现及自动化验收完成；真实生产验收另列 |
| C02 官网 | backend/templates/、backend/static/ | 全页面、真实 API、响应式、可访问状态和深浅色；本地实现及自动化验收完成；真实生产验收另列 |
| C03 桌面可靠性 | code/relay/engine.py、checks/、relay_config.py；tests/test_diagnostics* | 动态 VPN、默认环境、命令失败和回滚模拟回归；本地实现及自动化验收完成；真实生产验收另列 |
| C04 桌面商业能力 | code/relay/commercial/、network-doctor-menu.py；tests/test_commercial* | Keychain、签名、浏览器授权、30天历史、对比、脱敏导出；本地实现及自动化验收完成；真实生产验收另列 |
| C05 运行与交付 | scripts/、deploy/commercial/、apps/macos/、docs/、README | 锁依赖、启动/构建、API消费者联测、浏览器验收、发布/回滚说明；本地实现及自动化验收完成；真实生产验收另列 |

依赖：C01 与 C04/C02 共享 SPEC-001；变更先沟通契约。现有诊断报告和运行配置不被移除。无需重新向用户申请本地可逆实施许可。

回退：保留原 code/ 入口及现有安装目录；本轮验证不替换当前正在运行的用户版本，不修改系统网络。服务数据独立在 backend/.runtime；正式发布回退保留订单账本、支付回调和密钥，不反向删除已完成的交易。

最终验收见 [QA-001](../../quality/reports/QA-001-commercialization.md)；本次没有公开部署、收款或安装覆盖。
