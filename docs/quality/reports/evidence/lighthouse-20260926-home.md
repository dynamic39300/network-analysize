# Lighthouse 本机首页实测

- 报告时间：2026-09-26 00:28:14 UTC（北京时间 08:28:14）。Lighthouse 13.5.0，独立 headless Chrome。
- 目标：`http://127.0.0.1:8016/`，本机 Django development，匿名首页；不是公开生产域名或真实用户性能样本。
- 分数：Performance **79**、Accessibility **100**、Best Practices **96**、SEO **100**。无 Lighthouse runtimeError。
- LCP 2.2 s，TBT 830 ms；图片约 905 KiB、开发静态缓存、JS 压缩及页面未使用的代码影响性能。当前结果不能外推生产 CDN/压缩/缓存条件。
- 唯一 console 审计错误为 `/api/v1/me` 的预期匿名 HTTP 401，不是运行时脚本异常。没有为了分数掩盖此响应。
- 实验审计 `label-content-name-mismatch` 指出手机菜单可见文字“菜单”与 aria-label“展开导航”不完全匹配；应在后续前端修订中统一可见名称与无障碍名称。

原始证据为同目录 `lighthouse-20260926-home.json`。生成命令：

```sh
CHROME_PATH='/Users/eeo/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' \
  npx --yes lighthouse http://127.0.0.1:8016/ \
  --chrome-flags='--headless=new --no-sandbox' \
  --only-categories=performance,accessibility,best-practices,seo \
  --output=json --output=html --output-path=/tmp/relay-lighthouse --quiet
```

HTML 副本保存在本机 `/tmp/relay-lighthouse.report.html`，未重复提交体积较大的内嵌报告。
