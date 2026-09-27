/* First-party UI. No analytics, external fonts, token storage, or payment-URL
   sharing. Account data is escaped before entering a template. */
(() => {
  "use strict";

  const main = document.getElementById("main");
  const dialog = document.getElementById("action-dialog");
  const state = {
    config: null,
    me: null,
    planId: "yearly",
    order: null,
    poll: null,
  };
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const route = path === "/" ? "/" : `${path}/`;
  let toastTimer;
  let modalReturnFocus;
  let quoteSequence = 0;

  const escape = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  const money = (amount) =>
    new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: "CNY",
      maximumFractionDigits: Number(amount) % 100 ? 2 : 0,
    }).format(Number(amount) / 100);
  const date = (seconds, time = true) =>
    seconds
      ? new Intl.DateTimeFormat("zh-CN", {
          timeZone: "Asia/Shanghai",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          ...(time
            ? {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit",
                hour12: false,
              }
            : {}),
        }).format(new Date(seconds * 1000))
      : "尚未开始";
  const statusNames = {
    eligible: "还未开始试用",
    trial: "试用中",
    paid: "已开通",
    expired: "已到期",
    pending: "待支付",
    closed: "已关闭",
    refunded: "已退款",
    requested: "已提交",
    reviewing: "审核中",
    approved: "已批准",
    processing: "退款处理中",
    succeeded: "已退款",
    failed: "处理失败",
    rejected: "未通过",
    open: "待处理",
    resolved: "已解决",
    waiting: "待补充信息",
  };
  const label = (status) => statusNames[status] || "处理中";
  const planById = (id) => state.config?.plans?.find((plan) => plan.id === id);
  const planName = (id) => planById(id)?.name || "NetCare Pro";
  const csrf = () =>
    document.cookie
      .split("; ")
      .find((item) => item.startsWith("csrftoken="))
      ?.split("=")
      .slice(1)
      .join("=") || "";
  const notice = (message, kind = "") =>
    `<div class="notice ${kind}" role="${kind === "error" ? "alert" : "status"}">${escape(message)}</div>`;

  function safeURL(value) {
    try {
      if (typeof value !== "string" || !value.trim()) return null;
      const url = new URL(value, location.origin);
      if (url.username || url.password) return null;
      if (url.protocol === "https:" || (url.origin === location.origin && url.protocol === "http:")) return url.href;
    } catch (_) {}
    return null;
  }

  async function api(endpoint, { method = "GET", data, timeout = 20000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(`/api/v1${endpoint}`, {
        method,
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
        headers: {
          Accept: "application/json",
          ...(method !== "GET"
            ? {
                "Content-Type": "application/json",
                "X-CSRFToken": decodeURIComponent(csrf()),
              }
            : {}),
        },
        ...(data !== undefined ? { body: JSON.stringify(data) } : {}),
      });
      let payload;
      try {
        payload = await response.json();
      } catch (_) {
        payload = null;
      }
      if (!response.ok) {
        const error = new Error(
          payload?.error?.message ||
            (response.status === 401
              ? "登录已过期，请重新登录。"
              : "服务暂时没有响应，请稍后重试。"),
        );
        if (response.status === 401 && state.me) {
          state.me = null;
          updateNav();
        }
        error.status = response.status;
        error.code = payload?.error?.code;
        throw error;
      }
      if (!payload) throw new Error("收到的响应不完整，请重试。");
      return payload;
    } catch (error) {
      if (error.name === "AbortError")
        throw new Error(
          method === "GET"
            ? "请求超时，请检查网络后重试。"
            : "请求超时，结果尚未确认。请先刷新状态，避免重复操作。",
        );
      if (error instanceof TypeError)
        throw new Error("暂时无法连接，请检查网络后重试。");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function flash(message, error = false) {
    const toast = document.getElementById("toast");
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.hidden = false;
    toastTimer = setTimeout(() => {
      toast.hidden = true;
    }, 5500);
  }

  function setMessage(container, message, kind = "error") {
    if (!container?.isConnected) {
      flash(message, kind === "error");
      return;
    }
    container.innerHTML = notice(message, kind);
  }

  function updateNav() {
    const account = document.getElementById("nav-account");
    account.href = state.me
      ? "/account/"
      : route === "/login/"
        ? location.pathname + location.search
        : loginURL();
    account.innerHTML = `${state.me ? "我的账号" : "登录账号"} <span aria-hidden="true">↗</span>`;
    document.querySelectorAll("[data-nav]").forEach((link) => {
      if (link.dataset.nav === route) link.setAttribute("aria-current", "page");
    });
  }

  function loginURL(next = route + location.search) {
    return `/login/?next=${encodeURIComponent(next)}`;
  }

  function nextURL() {
    const next = new URLSearchParams(location.search).get("next");
    if (
      !next ||
      !next.startsWith("/") ||
      next.startsWith("//") ||
      next.includes("\\")
    )
      return "/account/";
    const url = new URL(next, location.origin);
    return url.origin === location.origin && url.pathname !== "/login/"
      ? url.pathname + url.search
      : "/account/";
  }

  async function busy(button, action) {
    if (!button || button.disabled) return;
    const previous = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "请稍候…";
    try {
      return await action();
    } finally {
      if (button.isConnected) {
        button.disabled = button.dataset.locked === "true";
        button.removeAttribute("aria-busy");
        button.textContent = previous;
      }
    }
  }

  function openDialog(title, content) {
    clearTimeout(state.poll);
    if (!dialog.open) modalReturnFocus = document.activeElement;
    document.getElementById("dialog-content").innerHTML =
      `<div class="dialog-heading"><h2 id="dialog-title">${escape(title)}</h2><button class="dialog-close" type="button" data-close-dialog aria-label="关闭">×</button></div>${content}`;
    if (!dialog.open) dialog.showModal();
    dialog.querySelector("[data-close-dialog]").focus();
    dialog.querySelector("[data-close-dialog]").onclick = () => dialog.close();
  }

  dialog.addEventListener("close", () => {
    clearTimeout(state.poll);
    state.order = null;
    if (modalReturnFocus?.isConnected) modalReturnFocus.focus();
  });

  const title = (value) => {
    document.title =
      value === "NetCare"
        ? "NetCare · 看清连接的每一层"
        : `${value} · NetCare`;
  };

  const examples = {
    dns: {
      title: "连接正常，域名解析没有完成。", result: "优先检查 DNS", description: "这个示例中，局域网和公网连接正常；目标域名解析超时。先检查 DNS 配置，再判断应用本身。",
      evidence: [["本地连接", "正常"], ["公网连接", "正常"], ["域名解析", "超时"], ["目标服务", "待复测"]],
      detail: "解析失败不能证明目标服务宕机。保存当前 DNS 配置，确认 VPN 的域名分流策略，再做一次对照检测。"
    },
    proxy: {
      title: "问题出现在代理这一段。", result: "优先检查代理", description: "这个示例中，直连对照请求成功，使用当前代理的连接未完成。检查代理进程与端口，比反复重装应用更有依据。",
      evidence: [["本地连接", "正常"], ["域名解析", "正常"], ["直连对照", "可达"], ["代理连接", "未完成"]],
      detail: "这是请求路径的差异，不代表关闭代理一定适合你的网络。先核对应用实际使用的代理设置，修复前保存基线。"
    },
    service: {
      title: "本地链路可达，继续核对目标服务。", result: "保留服务端证据", description: "这个示例中，DNS、连接和 TLS 握手都已完成，目标服务返回异常响应。保留响应状态并核对官方公告。",
      evidence: [["域名解析", "正常"], ["网络连接", "可达"], ["TLS 握手", "完成"], ["服务响应", "异常"]],
      detail: "可达不等于业务可用。服务公告也不能单独排除本机问题，需要结合相同时间的客户端错误和实际请求。"
    }
  };
  let exampleId = "dns";
  function reportDemo() {
    return `<div class="report-workbench"><div class="report-toolbar"><div><strong>诊断报告示例</strong><p>切换场景，看看证据如何形成结论</p></div><div class="report-tabs" role="group" aria-label="选择诊断示例"><button data-example="dns" aria-pressed="true">DNS 异常</button><button data-example="proxy" aria-pressed="false">代理异常</button><button data-example="service" aria-pressed="false">服务异常</button></div></div><div id="report-content" class="report-content" aria-live="polite"></div><div class="report-bottom"><p>这是交互示例，不会检测或修改你的网络。实际检测请在 Mac 客户端运行。</p><button id="copy-example" type="button">复制示例报告 ↗</button></div></div>`;
  }
  function bindReport() {
    const show = (id) => {
      exampleId = id;
      const item = examples[id];
      main.querySelectorAll('[data-example]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.example === id)));
      document.getElementById('report-content').innerHTML = `<div class="report-summary"><span class="report-result">${item.result}</span><h3>${item.title}</h3><p>${item.description}</p></div><div><ul class="evidence-list">${item.evidence.map(([key,value]) => `<li><span>${key}</span><strong class="${['正常','可达','完成'].includes(value)?'':'uncertain'}">${value}</strong></li>`).join('')}</ul><details class="report-disclosure"><summary>查看下一步与判断边界</summary><p>${item.detail}</p></details></div>`;
    };
    main.querySelectorAll('[data-example]').forEach(button => button.onclick = () => show(button.dataset.example));
    show(exampleId);
    document.getElementById('copy-example').onclick = async () => {
      const item = examples[exampleId];
      const report = `NetCare 诊断报告示例（非当前网络检测）\n\n${item.title}\n${item.description}\n\n${item.evidence.map(row=>row.join('：')).join('\n')}\n\n${item.detail}`;
      try { await navigator.clipboard.writeText(report); flash('示例报告已复制。'); }
      catch (_) { openDialog('复制示例报告', `<div class="field"><label for="example-copy">请选择并复制以下示例</label><textarea id="example-copy" readonly rows="10">${escape(report)}</textarea></div>`); document.getElementById('example-copy').select(); }
    };
  }
  function home() {
    title("NetCare");
    main.innerHTML = `<div class="page-width"><section class="hero"><div class="hero-copy"><p class="eyebrow">为 Mac 上的每一次连接</p><h1>网络不通，<br>先看清问题在哪。</h1><p>从本地连接到目标服务，逐层检查。<br>让每一次修复，都有依据。</p><div class="button-row"><a class="btn btn-primary" href="/download/">获取 Mac 版 <span aria-hidden="true">↗</span></a><a class="btn btn-secondary" href="#report-example">查看诊断示例</a></div></div><figure class="hero-art"><img src="/static/relay-logo.png" width="360" height="360" alt="NetCare 银色信号弧应用图标" fetchpriority="high"></figure></section><section class="foundation-strip" aria-label="产品原则"><div><strong>基础诊断永久免费</strong><p>无需账号，也能开始检查</p></div><div><strong>诊断数据留在本机</strong><p>分享之前，由你决定</p></div><div><strong>修复有确认、有回退</strong><p>先保存基线，再验证结果</p></div></section><section class="section" id="report-example"><h2>不只告诉你“连接失败”。</h2><p>分清连接、解析、代理与服务响应，把下一步说清楚。</p>${reportDemo()}</section><section class="principles"><div><h2>让排查更有条理，<br>让改动更有把握。</h2><p>网络问题常常不在同一层。NetCare 保留每一项检查的依据，也明确告诉你哪些还不能确认。</p><a class="btn btn-quiet" href="/features/">了解全部功能 ↗</a></div><div><article class="principle-item"><h3>从现象找到值得检查的地方</h3><p>核对接口、路由、DNS 与代理，结合目标请求判断故障范围。</p></article><article class="principle-item"><h3>先看建议，再决定是否修复</h3><p>修复前重新检测并保存相关配置；失败时尝试恢复，明确展示结果。</p></article></div></section><section class="section-cta"><div><h2>今天查清楚，下次有对照。</h2><p>Pro 增加 30 天本地历史、快照对比与脱敏诊断包。基础检查一直免费。</p></div><a class="btn btn-secondary" href="/pricing/">查看 Free 与 Pro ↗</a></section></div>`;
    bindReport();
  }
  function features() {
    title('功能');
    main.innerHTML = `<div class="page-width"><header class="page-heading"><h1>把连接拆开看，<br>把问题连起来理解。</h1><p>基础排查、修复与报告属于 Free。需要回看和对照时，再选择 Pro。</p></header><div class="feature-overview"><article class="feature-card"><p class="feature-index">NetCare Free</p><h2>沿着真实的请求路径检查。</h2><p>区分本地接口、默认路由、DNS、VPN、代理与目标服务；没有足够证据时，保留“未确认”。</p><div class="feature-route" aria-label="检查范围"><span>本地连接</span><span>DNS</span><span>VPN / 代理</span><span>目标服务</span></div></article><article class="feature-card"><p class="feature-index">NetCare Free</p><h2>修复前，先保留退路。</h2><p>只提供内建的修复动作。经过你的确认，再保存相关配置、执行并验证；失败和回滚失败都会明确显示。</p></article><article class="feature-card wide"><div><p class="feature-index">NetCare Pro</p><h2>让这一次排查，<br>成为下一次的依据。</h2></div><p>保留 30 天本地历史，比较两次诊断快照，导出脱敏 HTML / JSON 诊断包。到期后仍保留已有记录，不影响免费检查。</p></article></div><section class="section"><h2>一份报告，同时有结论和边界。</h2><p>点击示例场景，查看不同证据下的建议。</p>${reportDemo()}</section><section class="feature-privacy"><h2>你的网络信息，默认留在你的 Mac。</h2><p>诊断不默认上传。Pro 导出会脱敏地址、SSID 与 URL 中的敏感信息；提交支持前仍可自行检查文件。账号服务只负责身份、权益与订单。</p><a href="/privacy/" class="btn btn-quiet">阅读隐私说明 ↗</a></section><section class="section-cta"><div><h2>从免费诊断开始。</h2><p>无需登录即可使用基础功能。Pro 试用由你主动开启。</p></div><a class="btn btn-primary" href="/download/">获取 Mac 版 ↗</a></section></div>`;
    bindReport();
  }

  function faq(items) {
    return items
      .map(
        ([question, answer]) =>
          `<details><summary>${escape(question)}</summary><p>${escape(answer)}</p></details>`,
      )
      .join("");
  }

  function pricing() {
    title("价格");
    const plans = state.config.plans.filter(plan => ['monthly','yearly'].includes(plan.id));
    if (!planById(state.planId)) state.planId = plans[0]?.id;
    main.innerHTML = `<div class="page-width"><header class="page-heading"><h1>基础检查，始终免费。<br>多一份对照，选择 Pro。</h1><p>主动购买，主动续期。不自动扣款；提前续费保留剩余权益。</p></header><div class="plan-layout"><section class="free-plan"><p class="plan-name">NetCare Free</p><h2>把当前问题查清楚。</h2><p class="plan-description">日常网络排查，无需账号。</p><div class="free-price">¥0<span>永久免费</span></div><ul class="benefits"><li>基础网络与服务检查</li><li>菜单栏网络监测</li><li>结果查看与基础报告</li><li>用户确认后的安全修复</li></ul><a class="btn btn-secondary full-width" href="/download/">获取 Mac 版</a></section><section class="pro-plan"><p class="plan-name">NetCare Pro</p><h2>把排查经验留下来。</h2><p class="plan-description">在 Free 之上，增加本地历史与诊断协作。</p><ul class="benefits"><li>30 天本地诊断历史</li><li>诊断快照对比</li><li>脱敏 HTML / JSON 诊断包</li></ul><div class="plan-options" role="group" aria-label="选择 Pro 购买期限">${plans.map(plan => `<button type="button" class="plan-option ${plan.id===state.planId?'is-selected':''}" data-plan="${escape(plan.id)}" aria-pressed="${plan.id===state.planId}"><span class="plan-option-title">${plan.id==='yearly'?'年度':'月度'}</span><div class="plan-price">${money(plan.amount)}<span> / ${plan.months===1?'月':'年'}</span></div><p>${plan.months} 个日历月</p></button>`).join('')}</div><div id="checkout-content"></div></section></div><section class="pricing-trial"><div><h3>还没决定？先体验 14 天 Pro。</h3><p>每账号一次，由你主动开启。到期恢复 Free，不自动扣款。</p></div><a class="btn btn-secondary" href="${state.me?'/account/':loginURL('/account/')}">查看试用资格</a></section><section class="faq-section"><h2>购买前，先了解这些。</h2>${faq([
      ['需要账号才能诊断网络吗？','不需要。基础检测、菜单栏监测、结果和基础报告、安全修复永久免费。账号服务暂时不可用，也不会阻止本地 Free 检查。'],
      ['提前续期会损失剩余时间吗？','不会。购买的月度或年度期限接在现有连续权益之后；已到期则从成功开通重新起算。具体时间会在确认购买前显示。'],
      ['试用怎样开始？','登录后主动点击开始 14 天 Pro 试用，并确认后起算。注册和下载都不会自动开始。试用期内购买保留剩余时间；直接购买的账号不再追加一次免费试用。'],
      ['断网或 Pro 到期，会发生什么？','已验证的 Pro 权益可在其有效期内最多离线使用 7 天。到期或需要联网验证时恢复 Free，保留已有记录，停止新的付费操作。'],
      ['为什么现在无法真实购买？','真实支付只有在商户接入并验证后才会开放。开发环境的模拟支付不产生扣款，不能当作已经完成正式上线。']
    ])}</section></div>`;
    main.querySelectorAll('[data-plan]').forEach(button => button.onclick = () => {
      state.planId = button.dataset.plan;
      main.querySelectorAll('[data-plan]').forEach(item => {item.classList.toggle('is-selected',item===button);item.setAttribute('aria-pressed',String(item===button));});
      checkout();
    });
    checkout();
  }

  function checkout() {
    const container = document.getElementById("checkout-content");
    if (!state.config.termsVersion) {
      container.innerHTML = notice(
        "购买条款暂时无法读取，请刷新页面后重试。",
        "error",
      );
      return;
    }
    if (!state.me) {
      container.innerHTML = `${!state.config.payments.wechat && !state.config.payments.alipay ? notice("真实购买暂未开放。登录后可查看试用资格；本地演示付款不会产生扣款。") : ""}<p class="small">Pro 将绑定你的账号，供本人多台 Mac 使用。</p><a class="btn btn-primary full-width" href="${loginURL("/pricing/")}">登录后购买</a>`;
      return;
    }
    const channels = [
      ["wechat", "微信支付"],
      ["alipay", "支付宝"],
      ["simulated", "演示支付（不产生真实扣款）"],
    ].filter(
      ([id]) =>
        state.config.payments[id] &&
        (id !== "simulated" || state.config.environment === "development"),
    );
    const plan = planById(state.planId);
    container.innerHTML = `<p class="small account-email">购买账号：${escape(state.me.account.email)}</p><div id="quote-preview" class="expiry-preview" role="status">正在计算购买后的期限…</div>${state.me.entitlement.status === "eligible" ? notice("你还未开始 Pro 试用，可先到账号中心主动开启。直接购买将立即开通付费期限，不再追加免费试用。") : ""}${channels.length ? `<form id="checkout-form"><div class="field"><label for="payment-channel">付款方式</label><select id="payment-channel" name="channel">${channels.map(([id, text]) => `<option value="${id}">${text}</option>`).join("")}</select></div><label class="consent"><input type="checkbox" name="consent" required><span>我已阅读 <a class="text-link" href="/terms/" target="_blank" rel="noopener">服务与退款条款</a>。本次为主动购买，不自动扣款。</span></label><button class="btn btn-primary full-width" type="submit">购买${escape(plan.name)} · ${money(plan.amount)}</button><div id="checkout-message"></div></form>` : notice("真实支付尚未开放。已购权益仍可查询；Free 基础诊断不受影响。")}`;
    loadQuote(state.planId);
    const form = document.getElementById("checkout-form");
    if (!form) return;
    const idempotencyKey = crypto.randomUUID();
    form.onsubmit = (event) => {
      event.preventDefault();
      busy(form.querySelector("button[type=submit]"), async () => {
        try {
          const result = await api("/orders", {
            method: "POST",
            data: {
              planId: plan.id,
              channel: new FormData(form).get("channel"),
              idempotencyKey,
              acceptedTerms: form.elements.consent.checked,
              termsVersion: state.config.termsVersion,
              expectedAmount: plan.amount,
              expectedCurrency: plan.currency,
            },
          });
          paymentDialog(result);
        } catch (error) {
          if (["price_changed", "terms_updated"].includes(error.code)) {
            showCheckoutUpdate(
              error.message,
              document.getElementById("checkout-message"),
            );
            return;
          }
          setMessage(
            document.getElementById("checkout-message"),
            error.message,
          );
        }
      });
    };
  }

  function showCheckoutUpdate(message, target) {
    target.innerHTML = `${notice(message, "error")}<button type="button" class="btn btn-secondary btn-small" id="refresh-purchase-terms">查看最新方案与条款</button>`;
    const purchase = document.querySelector(
      "#checkout-form button[type=submit]",
    );
    if (purchase) {
      purchase.disabled = true;
      purchase.dataset.locked = "true";
    }
    document.getElementById("refresh-purchase-terms").onclick = (event) =>
      busy(event.currentTarget, async () => {
        try {
          state.config = await api("/config");
          pricing();
          const message = document.getElementById("checkout-message");
          if (message)
            setMessage(
              message,
              "方案已刷新。请核对金额、期限与条款，重新同意后再购买。",
              "",
            );
        } catch (error) {
          flash(error.message, true);
        }
      });
  }

  async function loadQuote(planId) {
    const seq = ++quoteSequence;
    const target = document.getElementById("quote-preview");
    const purchase = document.querySelector(
      "#checkout-form button[type=submit]",
    );
    if (purchase) purchase.disabled = true;
    try {
      const result = await api(
        `/orders/quote?planId=${encodeURIComponent(planId)}`,
      );
      if (seq !== quoteSequence || !target.isConnected) return;
      const plan = planById(planId);
      if (result.amount !== plan.amount || result.currency !== plan.currency) {
        showCheckoutUpdate("方案价格已更新，请先查看最新价格再购买。", target);
        return;
      }
      target.innerHTML = `预计购买期限（北京时间）<strong>${date(result.startsAt)} 至 ${date(result.endsAt)}</strong><span>支付确认后，以订单实际发放时间为准。</span>`;
      if (purchase) purchase.disabled = false;
    } catch (error) {
      if (seq !== quoteSequence || !target.isConnected) return;
      target.innerHTML = `${notice(`暂时无法计算购买期限。${error.message}`, "error")}<button type="button" class="btn btn-secondary btn-small" id="retry-quote">重新计算</button>`;
      document.getElementById("retry-quote").onclick = () => loadQuote(planId);
    }
  }

  function drawQR(value) {
    const holder = document.getElementById("payment-qr");
    try {
      const qr = window.qrcode(0, "M");
      qr.addData(value);
      qr.make();
      const count = qr.getModuleCount();
      const scale = 5,
        quiet = 4;
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = (count + quiet * 2) * scale;
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", "请用微信扫描付款二维码");
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#000000";
      for (let row = 0; row < count; row++)
        for (let col = 0; col < count; col++)
          if (qr.isDark(row, col))
            ctx.fillRect(
              (col + quiet) * scale,
              (row + quiet) * scale,
              scale,
              scale,
            );
      holder.replaceChildren(canvas);
    } catch (_) {
      holder.innerHTML = notice(
        "二维码暂时无法显示。请关闭后从订单重新打开付款。",
        "error",
      );
    }
  }

  function paymentDialog(result) {
    const { order, payment } = result;
    state.order = order;
    const complete = ["paid", "refunded", "closed"].includes(order.status);
    const redirect = payment?.kind === "redirect" ? safeURL(payment.url) : null;
    openDialog(
      complete ? label(order.status) : "完成购买",
      `<div class="payment-summary"><div>${escape(planName(order.planId))}<p class="small">${escape(state.me.account.email)}</p></div><strong>${money(order.amount)}</strong></div><div id="payment-state">${order.status === "paid" ? notice(`已开通，有效至 ${date(order.endsAt)}（北京时间）。回到 App 同步已购权益即可。`, "success") : order.status === "refunded" ? notice("此订单已退款。其他订单的有效期请在账号中心查看。") : order.status === "closed" ? notice("此订单已关闭，不能继续付款。") : payment?.kind === "qr" && payment.url ? `<div id="payment-qr" class="payment-qr"></div><p class="payment-instruction">请使用微信扫描二维码付款</p>` : redirect ? `<a class="btn btn-primary full-width" href="${escape(redirect)}" target="_blank" rel="noopener noreferrer">前往支付宝付款 <span aria-hidden="true">↗</span></a>` : payment?.kind === "simulated" && state.config.environment === "development" ? `${notice("演示环境：以下操作仅用于验证流程，不产生真实付款。")}<button id="simulate-payment" class="btn btn-secondary full-width">模拟付款成功</button>` : notice("付款入口暂时不可用。请稍后从订单页面重试。", "error")}</div><p class="order-id">订单编号：${escape(order.id)}</p>${!complete ? '<p class="waiting-copy">付过款却未开通？先刷新付款状态，无需再次付款。关闭本页后也可以在订单中继续查看。</p><button id="sync-payment" class="btn btn-secondary full-width">我已付款，刷新状态</button>' : '<a class="btn btn-primary full-width" href="/account/">查看我的授权</a>'}<div id="payment-message"></div><a class="btn btn-quiet full-width" href="/orders/">查看全部订单</a>`,
    );
    state.order = order;
    if (payment?.kind === "qr" && payment.url && !complete) drawQR(payment.url);
    const sync = document.getElementById("sync-payment");
    if (sync)
      sync.onclick = () =>
        busy(sync, async () => {
          try {
            const updated = await api(
              `/orders/${encodeURIComponent(order.id)}/sync`,
              { method: "POST" },
            );
            if (updated.order.status !== "pending") {
              state.me = await api("/me");
              paymentDialog(updated);
            } else
              setMessage(
                document.getElementById("payment-message"),
                "尚未确认付款。若已付款，请稍候再查，不要重复支付。",
                "",
              );
          } catch (error) {
            setMessage(
              document.getElementById("payment-message"),
              error.message,
            );
          }
        });
    const simulate = document.getElementById("simulate-payment");
    if (simulate)
      simulate.onclick = () =>
        busy(simulate, async () => {
          try {
            const updated = await api(
              `/orders/${encodeURIComponent(order.id)}/simulate`,
              { method: "POST" },
            );
            state.me = await api("/me");
            paymentDialog(updated);
          } catch (error) {
            setMessage(
              document.getElementById("payment-message"),
              error.message,
            );
          }
        });
    if (!complete) pollOrder(order.id, 0);
  }

  function pollOrder(id, attempt) {
    clearTimeout(state.poll);
    if (attempt >= 30) return;
    state.poll = setTimeout(
      async () => {
        if (!dialog.open || state.order?.id !== id) return;
        if (document.hidden) {
          pollOrder(id, attempt + 1);
          return;
        }
        try {
          const updated = await api(`/orders/${encodeURIComponent(id)}`);
          if (!dialog.open || state.order?.id !== id) return;
          if (updated.order.status !== "pending") {
            state.me = await api("/me");
            paymentDialog(updated);
            return;
          }
        } catch (_) {
          /* Manual sync remains available; do not overwrite payment UI. */
        }
        pollOrder(id, attempt + 1);
      },
      attempt < 6 ? 4000 : 10000,
    );
  }

  function login() {
    title("登录");
    if (!state.config.termsVersion || !state.config.privacyVersion) {
      main.innerHTML =
        '<div class="standalone-error"><h1>服务说明暂时无法读取。</h1><p>请刷新页面后再登录，你的账号与权益不会受到影响。</p><a class="btn btn-secondary" href="/login/">重新加载</a></div>';
      return;
    }
    if (state.me) {
      location.replace(nextURL());
      return;
    }
    main.innerHTML = `<div class="page-width auth-layout"><div class="auth-description"><img class="auth-logo" src="/static/relay-logo.png" width="108" height="108" alt="NetCare"><h1>一个账号，<br>连接你的每台 Mac。</h1><p>管理 Pro 权益、登录设备与订单。<br>基础网络诊断始终无需账号。</p></div><section class="auth-card"><h2>登录或创建账号</h2><p>首次验证邮箱后创建账号，不会自动开始试用。</p>${state.config.environment === "development" ? notice("本地演示默认将验证码保存在运行端的本机邮件目录；如部署者配置了邮件发送，请查收邮箱。网页不会返回验证码。") : ""}<form id="login-form"><div class="field"><label for="email">邮箱地址</label><input id="email" name="email" type="email" autocomplete="email" placeholder="you@example.com" required maxlength="254"></div><label class="consent"><input name="consent" type="checkbox" required><span>我已阅读并同意 <a class="text-link" href="/terms/" target="_blank" rel="noopener">服务条款</a> 与 <a class="text-link" href="/privacy/" target="_blank" rel="noopener">隐私说明</a>。</span></label><button type="submit" class="btn btn-primary full-width">获取验证码</button><div id="login-message"></div></form><div id="verify-step" hidden></div></section></div>`;
    const form = document.getElementById("login-form");
    form.onsubmit = (event) => {
      event.preventDefault();
      busy(form.querySelector("button"), async () => {
        const email = new FormData(form).get("email").trim();
        try {
          await api("/auth/request-code", { method: "POST", data: { email } });
          form.hidden = true;
          const step = document.getElementById("verify-step");
          step.hidden = false;
          step.innerHTML = `<p class="small">${state.config.environment === "development" ? "验证码请求已受理。开发默认保存在运行端本机邮件目录；如已配置邮件发送，请查收邮箱。收件地址：" : "如果该邮箱可接收邮件，验证码已发送至"}<br><strong class="account-email">${escape(email)}</strong></p><form id="verify-form"><div class="field"><div class="inline-label"><label for="login-code">邮件验证码</label><button type="button" id="change-email">修改邮箱</button></div><input id="login-code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" minlength="6" placeholder="6 位数字" required></div><button class="btn btn-primary full-width" type="submit">验证并登录</button><div id="verify-message"></div></form><button class="btn btn-quiet" id="resend-code" disabled>60 秒后重新发送</button><p class="field-hint">${state.config.environment === "development" ? "开发默认请查看运行服务的本机邮件目录；如已配置邮件发送，请查收邮箱。" : "未收到？请检查垃圾邮件。"}验证码只用于登录，请勿转发。</p>`;
          document.getElementById("login-code").focus();
          const resend = document.getElementById("resend-code");
          let remaining = 60;
          let interval;
          const countdown = () => {
            clearInterval(interval);
            remaining = 60;
            resend.disabled = true;
            resend.textContent = "60 秒后重新发送";
            interval = setInterval(() => {
              remaining--;
              if (!resend.isConnected || remaining <= 0) {
                clearInterval(interval);
                resend.disabled = false;
                resend.textContent = "重新发送验证码";
              } else resend.textContent = `${remaining} 秒后重新发送`;
            }, 1000);
          };
          countdown();
          document.getElementById("change-email").onclick = () => {
            clearInterval(interval);
            step.hidden = true;
            form.hidden = false;
            document.getElementById("email").focus();
          };
          resend.onclick = () =>
            busy(resend, async () => {
              try {
                await api("/auth/request-code", {
                  method: "POST",
                  data: { email },
                });
                flash(state.config.environment === "development" ? "验证码请求已再次受理，请查看已配置的邮件接收位置。" : "验证码已重新发送，请检查邮箱。");
                return true;
              } catch (error) {
                setMessage(
                  document.getElementById("verify-message"),
                  error.message,
                );
              }
            }).then((sent) => { if (sent) countdown(); });
          const verifyForm = document.getElementById("verify-form");
          verifyForm.onsubmit = (event) => {
            event.preventDefault();
            busy(verifyForm.querySelector("button[type=submit]"), async () => {
              try {
                state.me = await api("/auth/verify-code", {
                  method: "POST",
                  data: {
                    email,
                    code: new FormData(verifyForm).get("code").trim(),
                    acceptedTerms: verifyForm.elements.renewedConsent
                      ? verifyForm.elements.renewedConsent.checked
                      : form.elements.consent.checked,
                    termsVersion: state.config.termsVersion,
                    privacyVersion: state.config.privacyVersion,
                  },
                });
                clearInterval(interval);
                location.replace(nextURL());
              } catch (error) {
                if (
                  error.code === "terms_updated" ||
                  error.code === "agreement_required"
                ) {
                  try {
                    const config = await api("/config");
                    if (!config.termsVersion || !config.privacyVersion)
                      throw new Error("最新条款暂时无法读取，请稍后重试。");
                    state.config = config;
                    let updated = document.getElementById("renewed-consent");
                    if (!updated) {
                      updated = document.createElement("div");
                      updated.id = "renewed-consent";
                      verifyForm.prepend(updated);
                    }
                    updated.innerHTML = `${notice("服务说明已更新，请重新阅读并同意后继续验证。")}<label class="consent"><input type="checkbox" name="renewedConsent" required><span>我已阅读并同意最新的 <a class="text-link" href="/terms/" target="_blank" rel="noopener">服务条款 ${escape(config.termsVersion)}</a> 与 <a class="text-link" href="/privacy/" target="_blank" rel="noopener">隐私说明 ${escape(config.privacyVersion)}</a>。</span></label>`;
                    document.getElementById("verify-message").replaceChildren();
                  } catch (updateError) {
                    setMessage(
                      document.getElementById("verify-message"),
                      updateError.message,
                    );
                  }
                  return;
                }
                setMessage(
                  document.getElementById("verify-message"),
                  error.message,
                );
              }
            });
          };
        } catch (error) {
          setMessage(document.getElementById("login-message"), error.message);
        }
      });
    };
  }

  function accountShell(content, active = route) {
    return `<div class="page-width workspace"><nav class="account-nav" aria-label="账号导航">${[
      ["/account/", "我的授权"],
      ["/orders/", "订单与退款"],
      ["/download/", "下载与更新"],
      ["/support/", "帮助与反馈"],
    ]
      .map(
        ([url, text]) =>
          `<a href="${url}" ${active === url ? 'aria-current="page"' : ""}>${text}</a>`,
      )
      .join(
        "",
      )}<div class="nav-divider"></div><button type="button" id="logout">退出网页登录</button></nav><div>${content}</div></div>`;
  }

  function bindLogout() {
    const button = document.getElementById("logout");
    if (button)
      button.onclick = () =>
        busy(button, async () => {
          try {
            await api("/auth/logout", { method: "POST" });
            location.assign("/");
          } catch (error) {
            flash(error.message, true);
          }
        });
  }

  async function account() {
    title("我的授权");
    const entitlement = state.me.entitlement;
    const eligible = entitlement.status === "eligible";
    const pro = entitlement.tier === "pro" || ['trial','paid'].includes(entitlement.status);
    main.innerHTML = accountShell(`<header class="workspace-heading"><div><h1>我的 NetCare</h1><p class="account-email">${escape(state.me.account.email)}</p></div><button id="refresh-account" class="btn btn-secondary btn-small">同步权益</button></header><section class="license-panel"><div><div class="status-label">${pro?'NetCare Pro':'NetCare Free'} / ${label(entitlement.status)}</div><h2>${eligible?'从 Free 开始，按需体验 Pro。':pro?'为下一次诊断，保留更多线索。':'基础检查，一直都在。'}</h2><p class="license-expiry">${pro?`Pro 有效至 ${date(entitlement.validUntil)}（北京时间）`:eligible?'14 天 Pro 试用，由你主动开启。':'Pro 已到期，基础诊断与已有记录仍保留。'}</p>${entitlement.status==='trial'&&entitlement.paidUntil?`<p class="small">试用结束 ${date(entitlement.trialEndsAt)} 后接续已购期限。</p>`:''}</div><div class="button-row">${eligible?'<button id="start-trial" class="btn btn-primary" type="button">开始 14 天 Pro 试用</button>':''}<a class="btn ${eligible?'btn-secondary':'btn-primary'}" href="/pricing/">${pro?'续期 Pro':'查看 Pro 方案'}</a></div></section><div class="account-note"><p>不自动扣款，同一账号供本人多台 Mac 使用。Pro 到期不删除已有记录。已验证权益最多离线使用 7 天，且不超过实际到期时间。</p></div><div class="section-title"><h2>已登录的电脑</h2><p>管理设备访问，不设置台数额度</p></div><div id="sessions" aria-live="polite"><div class="skeleton-line"></div><div class="skeleton-line"></div><span class="sr-only">正在加载登录会话</span></div>`);
    bindLogout();
    document.getElementById('refresh-account').onclick = event => busy(event.currentTarget, async () => {
      try { state.me = await api('/me'); await account(); flash('已同步最新权益。'); } catch(error) { flash(error.message,true); }
    });
    const trial = document.getElementById('start-trial');
    if (trial) trial.onclick = () => {
      openDialog('现在开始 14 天 Pro 试用？', `<p class="dialog-copy">成功开通后立即起算，每个账号只有一次试用。无需绑定支付方式，到期自动恢复 Free，不自动扣款。</p><button id="confirm-trial" class="btn btn-primary full-width" type="button">确认开始试用</button><div id="trial-message"></div>`);
      document.getElementById('confirm-trial').onclick = event => busy(event.currentTarget, async () => {
        try { state.me=await api('/trial/start',{method:'POST'}); dialog.close(); await account(); flash('Pro 试用已开启。'); }
        catch(error) { setMessage(document.getElementById('trial-message'),error.message); }
      });
    };
    await loadSessions();
  }

  async function loadSessions() {
    const container = document.getElementById("sessions");
    try {
      const result = await api("/sessions");
      container.innerHTML = result.sessions.length
        ? `<div class="sessions">${result.sessions.map((session) => `<article class="session-row"><div><h3>${escape(session.label || "Mac 电脑")} ${session.current ? '<span class="tag">当前会话</span>' : ""}</h3><p>最近连接 ${date(session.lastSeenAt)} · 北京时间</p></div><button class="btn btn-secondary btn-small" data-revoke="${escape(session.id)}" data-session-name="${escape(session.label || "此电脑")}">退出</button></article>`).join("")}</div>`
        : '<div class="empty-state"><h3>还没有登录的电脑</h3><p>在 App 中选择登录，浏览器确认后，电脑会显示在这里。</p><a class="btn btn-secondary btn-small" href="/download/">获取 Mac 版</a></div>';
      container.querySelectorAll("[data-revoke]").forEach(
        (button) =>
          (button.onclick = () => {
            openDialog(
              "退出此电脑？",
              `<p class="dialog-copy">${escape(button.dataset.sessionName)} 将需要重新登录。其他电脑和本地设置不会被删除。完全离线的设备将在签名授权到期后停止新的 Pro 操作，Free 不受影响。</p><button id="confirm-revoke" class="btn btn-danger full-width">退出这台电脑</button><div id="revoke-message"></div>`,
            );
            document.getElementById("confirm-revoke").onclick = (event) =>
              busy(event.currentTarget, async () => {
                try {
                  await api(
                    `/sessions/${encodeURIComponent(button.dataset.revoke)}/revoke`,
                    { method: "POST" },
                  );
                  dialog.close();
                  await loadSessions();
                  flash("已撤销该电脑的登录会话。");
                } catch (error) {
                  setMessage(
                    document.getElementById("revoke-message"),
                    error.message,
                  );
                }
              });
          }),
      );
    } catch (error) {
      container.innerHTML = `${notice(error.message, "error")}<button class="btn btn-secondary btn-small" id="retry-sessions">重新加载</button>`;
      document.getElementById("retry-sessions").onclick = loadSessions;
    }
  }

  async function orders() {
    title("订单与退款");
    main.innerHTML = accountShell(
      '<header class="workspace-heading"><div><h1>订单与退款</h1><p>每笔购买、权益期限与处理进度，都在这里。</p></div><a class="btn btn-secondary btn-small" href="/pricing/">购买</a></header><div id="order-list" aria-live="polite"><div class="page-loader"><div class="skeleton-line"></div><span>正在读取订单…</span></div></div>',
    );
    bindLogout();
    await loadOrders();
  }

  async function loadOrders() {
    const container = document.getElementById("order-list");
    try {
      const result = await api("/orders");
      container.innerHTML = result.orders.length
        ? `<div class="order-list">${result.orders.map((order) => `<article class="order-card"><div class="order-top"><div><h2>${escape(planName(order.planId))}</h2><p>${date(order.createdAt)} · 北京时间</p></div><div><div class="order-amount">${money(order.amount)}</div><span class="tag status-${escape(order.status)}">${label(order.status)}</span></div></div><div class="order-details"><div><span>支付方式</span>${{ wechat: "微信支付", alipay: "支付宝", simulated: "演示支付" }[order.channel] || "支付渠道"}</div>${order.startsAt ? `<div><span>生效时间（北京时间）</span>${date(order.startsAt)}</div><div><span>到期时间（北京时间）</span>${date(order.endsAt)}</div>` : "<div><span>授权期限</span>付款确认后发放</div>"}${order.refundStatus ? `<div><span>退款进度</span>${label(order.refundStatus)}</div>` : ""}</div><div class="button-row">${order.status === "pending" ? `<button class="btn btn-primary btn-small" data-pay-order="${escape(order.id)}">继续付款 / 查单</button>` : ""}${order.status === "paid" && !order.refundStatus ? `<button class="btn btn-secondary btn-small" data-refund-order="${escape(order.id)}">申请退款</button>` : ""}<a class="btn btn-quiet btn-small" href="/support/?order=${encodeURIComponent(order.id)}">联系支持</a></div><p class="order-id">订单编号：${escape(order.id)}</p></article>`).join("")}</div>`
        : '<div class="empty-state"><h2>你的第一笔订单，还没开始。</h2><p>购买后，付款状态和使用期限会保存在这里。已经付款却没有记录？请确认登录的是购买时使用的邮箱。</p><div class="button-row"><a class="btn btn-primary" href="/pricing/">查看购买方案</a><a class="btn btn-secondary" href="/support/">联系支持</a></div></div>';
      container.querySelectorAll("[data-pay-order]").forEach(
        (button) =>
          (button.onclick = () =>
            busy(button, async () => {
              try {
                paymentDialog(
                  await api(
                    `/orders/${encodeURIComponent(button.dataset.payOrder)}`,
                  ),
                );
              } catch (error) {
                flash(error.message, true);
              }
            })),
      );
      container
        .querySelectorAll("[data-refund-order]")
        .forEach(
          (button) =>
            (button.onclick = () => refundDialog(button.dataset.refundOrder)),
        );
    } catch (error) {
      container.innerHTML = `${notice(error.message, "error")}<button class="btn btn-secondary" id="retry-orders">重新加载订单</button>`;
      document.getElementById("retry-orders").onclick = loadOrders;
    }
  }

  function refundDialog(id) {
    openDialog(
      "申请退款",
      `<p class="dialog-copy">申请不会立即取消其他订单的授权。我们会核查订单与退款规则，并在此订单中更新进度。</p><form id="refund-form"><div class="field"><label for="refund-reason">退款原因</label><textarea id="refund-reason" name="reason" placeholder="请描述遇到的问题，不要填写密码、验证码或付款敏感信息。" required minlength="5" maxlength="2000"></textarea></div><p class="field-hint">退款申请会被记录并核查；提交申请不等于退款已完成。重复付款、未开通或产品故障请说明具体情况。</p><button class="btn btn-primary full-width" type="submit">提交退款申请</button><div id="refund-message"></div></form>`,
    );
    const form = document.getElementById("refund-form");
    form.onsubmit = (event) => {
      event.preventDefault();
      busy(form.querySelector("button"), async () => {
        try {
          await api(`/orders/${encodeURIComponent(id)}/refund`, {
            method: "POST",
            data: { reason: new FormData(form).get("reason") },
          });
          dialog.close();
          await loadOrders();
          flash("退款申请已提交，可在订单中查看进度。");
        } catch (error) {
          setMessage(document.getElementById("refund-message"), error.message);
        }
      });
    };
  }

  function supportGuides() {
    return faq([
      ['NetCare 能修复所有网络问题吗？','不能。NetCare 帮助区分本地连接、DNS、VPN、代理与目标服务问题。没有足够证据时会保留未确认，不会把猜测写成结论。'],
      ['检测显示未完成，需要立即修改设置吗？','先查看具体失败的检查。检测命令异常、超时或缺少证据，都可能产生未完成状态；这不等于网络健康，也不等于应该立即修改配置。'],
      ['付款后，Pro 还没有开通','确认网页和客户端使用同一个邮箱，在订单中刷新付款状态，再同步权益。不要重复付款；仍有问题时关联订单提交反馈。'],
      ['浏览器确认了，客户端还在等待','保持 NetCare 客户端打开。授权请求只有 5 分钟有效；如果客户端停止等待，请从客户端重新发起，并使用新链接确认。'],
      ['怎样分享诊断报告？','基础报告可供个人排查，Pro 可导出脱敏 HTML / JSON 诊断包。分享前检查内容，删除密码、令牌、企业地址或不适合公开的网络信息。']
    ]);
  }

  async function support() {
    title("帮助与反馈");
    if (!state.me) {
      main.innerHTML = `<div class="page-width"><header class="page-heading"><h1>排查遇到问题，我们一起看。</h1><p>先看看常见问题。需要进一步帮助，登录后可以提交反馈并追踪进度。</p></header><div class="support-grid"><section>${supportGuides()}</section><section class="support-card"><h2>联系支持</h2><p>登录后提交问题，保留完整的沟通记录。付款相关问题可关联订单。</p><a class="btn btn-primary" href="${loginURL()}" >登录并提交反馈</a>${supportEmail()}</section></div></div>`;
      return;
    }
    const orderId = new URLSearchParams(location.search).get("order") || "";
    main.innerHTML = accountShell(
      `<header class="workspace-heading"><div><h1>帮助与反馈</h1><p>保留现象与复现步骤，帮助我们定位问题。</p></div></header><div class="support-grid"><section class="support-card"><h2>提交新反馈</h2><form id="support-form"><div class="field"><label for="ticket-subject">问题标题</label><input id="ticket-subject" name="subject" placeholder="例如：VPN 连接后 DNS 检查未完成" required minlength="2" maxlength="160"></div><div class="field"><label for="ticket-order">关联订单编号（选填）</label><input id="ticket-order" name="orderId" value="${escape(orderId)}" maxlength="100" placeholder="与付款有关时填写"></div><div class="field"><label for="ticket-body">具体情况</label><textarea id="ticket-body" name="body" placeholder="请写下 macOS 与 App 版本、操作步骤、原本希望发生什么，以及实际看到的结果。" required minlength="5" maxlength="5000"></textarea><span class="field-hint">请勿填写密码、验证码、完整支付信息或工作内容。提交内容只用于处理你的问题。</span></div><button class="btn btn-primary" type="submit">提交反馈</button><div id="support-message"></div></form></section><section><h2>常见问题</h2>${supportGuides()}${supportEmail()}</section></div><div class="section-title"><h2>我的反馈记录</h2><button id="refresh-tickets" class="btn btn-quiet btn-small">刷新进度</button></div><div id="ticket-list" aria-live="polite"><div class="skeleton-line"></div><span class="sr-only">正在加载反馈</span></div>`,
    );
    bindLogout();
    const form = document.getElementById("support-form");
    form.onsubmit = (event) => {
      event.preventDefault();
      busy(form.querySelector("button"), async () => {
        const data = new FormData(form);
        try {
          const payload = {
            subject: data.get("subject"),
            body: data.get("body"),
          };
          if (data.get("orderId").trim())
            payload.orderId = data.get("orderId").trim();
          await api("/support", { method: "POST", data: payload });
          form.reset();
          document.getElementById("ticket-order").value = "";
          setMessage(
            document.getElementById("support-message"),
            "反馈已收到，你可以在下方查看记录并补充信息。",
            "success",
          );
          await loadTickets();
        } catch (error) {
          setMessage(document.getElementById("support-message"), error.message);
        }
      });
    };
    document.getElementById("refresh-tickets").onclick = (event) =>
      busy(event.currentTarget, loadTickets);
    await loadTickets();
  }

  function supportEmail() {
    const email = state.config.supportEmail;
    return email && /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(email)
      ? `<p class="small">也可以发送邮件至 <a class="text-link" href="mailto:${encodeURIComponent(email)}">${escape(email)}</a></p>`
      : '<p class="small">正式客服邮箱尚未配置。你可以登录后通过此页提交问题并查看记录。</p>';
  }

  async function loadTickets() {
    const container = document.getElementById("ticket-list");
    try {
      const result = await api("/support");
      container.innerHTML = result.tickets.length
        ? `<div class="ticket-list">${result.tickets.map((ticket) => `<article class="ticket-card"><details><summary>${escape(ticket.subject)}</summary><div class="ticket-meta">${label(ticket.status)} · ${date(ticket.createdAt)}（北京时间）<br>编号：<span class="mono">${escape(ticket.id)}</span></div><div class="ticket-messages">${ticket.messages.map((message) => `<div class="ticket-message ${message.fromSupport ? "from-support" : ""}"><div class="message-meta">${message.fromSupport ? "NetCare 支持" : "我"} · ${date(message.createdAt)}</div>${escape(message.body)}</div>`).join("")}</div><form class="ticket-reply" data-ticket="${escape(ticket.id)}"><div class="field"><label for="reply-${escape(ticket.id)}">补充信息</label><textarea id="reply-${escape(ticket.id)}" name="body" required minlength="2" maxlength="5000" placeholder="在这里继续补充问题信息"></textarea></div><button class="btn btn-secondary btn-small" type="submit">发送补充</button><div class="reply-message"></div></form></details></article>`).join("")}</div>`
        : '<div class="empty-state"><h3>目前还没有反馈记录</h3><p>提交的问题会显示在这里，回复与进度都会保留。</p></div>';
      container.querySelectorAll("[data-ticket]").forEach(
        (form) =>
          (form.onsubmit = (event) => {
            event.preventDefault();
            busy(form.querySelector("button"), async () => {
              try {
                await api(
                  `/support/${encodeURIComponent(form.dataset.ticket)}/messages`,
                  {
                    method: "POST",
                    data: { body: new FormData(form).get("body") },
                  },
                );
                await loadTickets();
                const updated = [
                  ...container.querySelectorAll("[data-ticket]"),
                ].find((item) => item.dataset.ticket === form.dataset.ticket);
                if (updated) updated.closest("details").open = true;
                flash("补充信息已发送。");
              } catch (error) {
                setMessage(form.querySelector(".reply-message"), error.message);
              }
            });
          }),
      );
    } catch (error) {
      container.innerHTML = `${notice(error.message, "error")}<button id="retry-tickets" class="btn btn-secondary btn-small">重新加载</button>`;
      document.getElementById("retry-tickets").onclick = loadTickets;
    }
  }

  async function download() {
    title('下载与更新');
    const data=state.config.download;
    const url=data.available?safeURL(data.url):null;
    main.innerHTML=`<div class="page-width"><header class="page-heading"><h1>让连接的问题，<br>在 Mac 上看清楚。</h1><p>基础诊断无需登录。需要历史、对比与诊断包时，再开启 Pro。</p></header><div class="download-layout"><section class="download-lead"><img src="/static/relay-logo.png" width="88" height="88" alt="NetCare 应用图标"><h2>NetCare for Mac</h2><p>检查网络，理解原因，确认后修复。</p>${url?`<a class="btn btn-primary" href="${escape(url)}">下载 Mac 版 ${escape(data.version||'')} ↓</a>`:'<button class="btn btn-primary" type="button" disabled>正式安装包准备中</button><div class="notice">正式下载尚未开放。签名、公证与安装验证完成后，这里才会提供经过验证的安装包。</div>'}<div class="system-spec"><span>${url?'系统要求':'目标系统'}：macOS ${escape(data.minimumOS||'12.0')} 及以上</span><span>${escape(data.architecture||'arm64')} / Apple Silicon</span></div><p class="small">${url?'请按上方系统与芯片要求选择安装包。':'当前为开发目标，不代表所有系统版本已验收。'}Intel 与 Windows 正式版本尚未提供。</p></section><section><ol class="install-steps"><li><h3>从正式入口获取安装包</h3><p>下载开放后，从本页获取。签名未验证或来源不明的安装包，不应通过关闭系统保护来安装。</p></li><li><h3>打开 NetCare，运行基础检查</h3><p>先查看检测结论与原始依据。检查未完成时会明确显示，不把未知当作健康。</p></li><li><h3>核对建议，再确认修复</h3><p>修复会改变相关网络设置，请先核对作用范围。NetCare 保存相关配置，并在执行后重新验证。</p></li><li><h3>需要对照时，再启用 Pro</h3><p>登录账号后主动开启 14 天试用。浏览器确认设备，客户端自动完成登录；不需要手工复制凭证。</p></li></ol></section></div><section class="faq-section"><h2>安装与更新</h2>${faq([
      ['系统提示无法验证开发者，怎么办？','先确认来自本页正式下载入口并检查下载是否完整。仍被阻止时请联系支持，不要关闭系统安全保护。'],
      ['更新会删除我的记录吗？','正常更新应保留本地设置和记录。更新前退出旧版本，并保留重要诊断报告；若遇到异常，请记录版本与复现步骤。'],
      ['电脑不联网，还能使用吗？','Free 的本地检查不依赖账号连接。网络不可达的检查会如实显示失败或未完成，已有有效 Pro 授权最多离线验证 7 天。']
    ])}<div class="section-title"><h2>版本说明</h2></div><div id="release-notes"><p class="small">${data.available?'正在读取版本说明…':'正式版本发布后，将在这里同步更新内容和文件校验信息。'}</p></div></section></div>`;
    if(data.available){
      try {
        const release=await api('/releases/latest');
        document.getElementById('release-notes').innerHTML=`<p class="release-notes">${escape(release.notes||'暂无补充说明。')}</p>${release.sha256?`<p class="order-id">SHA-256：${escape(release.sha256)}</p>`:''}`;
      }catch(error){document.getElementById('release-notes').innerHTML=notice(error.message,'error');}
    }
  }

  async function authorize() {
    title('确认电脑登录');
    const request = new URLSearchParams(location.search).get('request');
    if (!request) { main.innerHTML=`<div class="page-width authorization"><h1>登录链接不完整。</h1><p>请回到 NetCare Mac 客户端，重新发起登录。</p><a class="btn btn-secondary" href="/account/">返回账号中心</a></div>`; return; }
    try {
      const result=await api(`/desktop/request?request=${encodeURIComponent(request)}`);
      main.innerHTML=`<div class="page-width authorization"><img class="auth-logo" src="/static/relay-logo.png" width="108" height="108" alt="NetCare"><h1>确认在这台 Mac 登录。</h1><p>只批准你刚刚在 NetCare 客户端发起的请求。</p><section class="auth-card"><dl><div><dt>账号</dt><dd>${escape(state.me.account.email)}</dd></div><div><dt>电脑名称</dt><dd>${escape(result.deviceName)}</dd></div><div><dt>请求有效至（北京时间）</dt><dd>${date(result.expiresAt)}</dd></div></dl><button class="btn btn-primary full-width" id="approve-desktop" type="button">确认登录到这台电脑</button><a class="btn btn-quiet full-width" href="/account/">暂不确认，返回账号</a><div id="authorize-message"></div></section><p class="small">确认后返回正在等待的 NetCare 客户端，无需复制任何登录凭证。</p></div>`;
      document.getElementById('approve-desktop').onclick=event=>busy(event.currentTarget,async()=>{
        try {
          const result=await api('/desktop/approve',{method:'POST',data:{request}});
          if(result.approved!==true) throw new Error('暂时无法确认授权结果，请回 App 重新发起。');
          document.querySelector('.authorization .auth-card').innerHTML=`${notice('登录已确认。请返回 NetCare 客户端，它会自动完成连接。','success')}<a href="/account/" class="btn btn-secondary full-width">查看账号与设备</a><p class="small">如果客户端已停止等待，请在客户端重新发起登录。本页面可以关闭。</p>`;
        }catch(error){setMessage(document.getElementById('authorize-message'),error.message);}
      });
    } catch(error) {
      main.innerHTML=`<div class="page-width authorization"><h1>暂时无法完成登录。</h1>${notice(error.message,'error')}<p>请回到 NetCare 客户端重新发起，再使用新的链接确认。</p><a class="btn btn-secondary" href="/account/">返回账号中心</a></div>`;
    }
  }

  function legal(kind) {
    const privacy=kind==='privacy';
    title(privacy?'隐私说明':'服务条款');
    const draft=state.config.environment!=='production'?notice('这是本地开发预览中的条款审阅稿。经营主体、服务联系信息与正式销售政策需要在公开发布前完成确认。'):'';
    main.innerHTML=`<article class="page-width legal-page"><h1>${privacy?'隐私说明':'服务条款'}</h1><p class="small">版本：${escape((privacy?state.config.privacyVersion:state.config.termsVersion)||'待公布')}</p>${draft}${privacy?`
      <p class="legal-intro">NetCare 用于诊断 Mac 的网络连接。诊断数据默认保存在本机；账号服务处理登录、权益、订单及你主动提交的支持记录。</p>
      <h2>网络诊断与本地记录</h2><p>运行检查时，客户端会读取相关网络接口、路由、DNS、代理与连接结果，按检查目标发起网络请求。目标服务因此可能接收到连接来源及该请求。我们不默认把本地诊断结果上传至账号服务。</p><p>Pro 在本机保留最多 30 天的诊断历史，支持快照对比与脱敏 HTML / JSON 导出。导出会处理地址、网络名称和 URL 中的敏感信息；文件仍可能包含你主动保留的技术信息，分享前请自行检查。到期不立即删除已有记录，既有本地保留规则继续适用。</p>
      <h2>账号与设备</h2><p>邮箱用于验证码登录与必要的服务沟通。服务器保存账号、试用和付费期限、电脑名称、会话创建与最近连接时间，用于同步权益和撤销设备会话。客户端登录凭证保存在 macOS Keychain，不通过网页展示或复制。</p><p>网站使用维持登录与防止跨站请求所需的 Cookie；浏览器本地存储只保存外观偏好。当前官网不加载广告追踪、第三方分析或远程字体。</p>
      <h2>订单、支付与支持</h2><p>服务保存订单、套餐、金额、渠道、付款与退款状态，以完成交易并处理争议。微信或支付宝按各自规则处理支付信息，我们不要求你在 NetCare 页面输入银行卡密码。</p><p>支持工单只包含你主动提交的标题、正文和关联订单。请勿粘贴密码、验证码、访问令牌、完整内部域名或未经允许分享的企业网络信息。开发模式下模拟付款不产生真实扣款。</p>
      <h2>访问、保存与删除</h2><p>你可以在账号中心查阅权益、订单与登录设备，撤销不再使用的电脑。账号更正、导出或删除请求可通过支持入口提出。为履行交易、处理争议或满足适用义务而需要保留的记录，其处理范围会在回应请求时说明。正式数据保留期限及服务提供方信息需要在公开上线前补全。</p>
      <h2>联系与运营信息</h2><p>${state.config.legalEntityName?`服务经营者：${escape(state.config.legalEntityName)}。`:'正式经营主体尚未公示，本地预览不构成正式运营服务。'}隐私问题请通过 <a href="/support/">支持页面</a> 提出。不要向任何人提供邮箱验证码或登录凭据。</p>
    `:`
      <p class="legal-intro">NetCare 帮助你理解连接故障并评估修复建议。它不能保证解决所有网络问题，也不能代替网络管理员的安全与访问政策。</p>
      <h2>Free 与 Pro</h2><p>Free 包含基础检测、菜单栏监测、结果查看、基础报告和经过用户确认的安全修复，永久免费且不依赖账号联网。Pro 增加 30 天本地历史、快照对比与脱敏诊断包。系统和芯片范围以 <a href="/download/">下载页面</a> 的实际发布信息为准。</p>
      <h2>修复与判断边界</h2><p>请在了解作用范围后确认修复。修复前保存相关配置并执行后验证，失败时尝试回滚；回滚也可能失败，此时会明确报告。目标服务可达不等于其业务正常，第三方服务公告也不能单独证明你的本机链路正常。</p>
      <h2>14 天 Pro 试用</h2><p>每个账号一次，只有在用户主动开始并成功开通后才计时。注册、下载或设备登录不自动开始试用。无需绑定付款方式，到期恢复 Free，不自动扣款。换电脑或重装不重置试用；直接购买付费期限后不再追加一次免费试用。</p>
      <h2>购买与续期</h2><p>初始方案为 Pro 月度 ¥12、年度 ¥98，分别对应 1 和 12 个日历月。实际金额以服务器返回的购买确认及订单为准；配置变化后需要重新确认。同一账号供本人多台 Mac 使用。</p><p>每笔付款由你主动发起，不设置自动代扣。有效期内续期保留剩余权益，已到期则从成功开通重新起算。付款状态以服务端核验为准，浏览器返回页面不代表已经付款。未开通时请先查单，避免重复购买。</p>
      <h2>离线、到期与撤销</h2><p>已验证的 Pro 签名授权最多允许 7 天离线使用，且不超过真实权益到期时间。超期需要联网同步。到期恢复 Free、保留已有记录，但不再开启新的 Pro 操作。撤销登录不会立即影响完全离线的签名授权，最长存在 7 天同步边界。</p>
      <h2>退款与问题处理</h2><p>可以在 <a href="/orders/">订单页面</a> 提交退款申请并查看进度。申请不等于已退款；渠道确认后只调整对应订单权益，其他未退款订单不受该笔撤销影响。重复付款、未开通或确认故障请提供订单号与复现情况。</p><p>正式销售前将公示明确的退款申请条件与处理政策；当前开发预览中的订单只用于验证流程，不构成真实交易。</p>
      <h2>发布、更新与联系</h2><p>只有经过签名、公证与发布验证的制品会进入正式下载入口。请勿通过关闭系统安全保护安装未知来源的软件。${state.config.legalEntityName?`服务经营者：${escape(state.config.legalEntityName)}。`:'经营主体及正式联系渠道将在公开收费前确认。'}使用、账号及购买问题可在 <a href="/support/">支持页面</a> 提交。</p>
    `}${supportEmail()}</article>`;
  }

  async function render() {
    if (
      ["/account/", "/orders/", "/desktop/authorize/"].includes(route) &&
      !state.me
    ) {
      location.replace(loginURL());
      return;
    }
    switch (route) {
      case "/":
        home();
        break;
      case "/features/":
        features();
        break;
      case "/pricing/":
        pricing();
        break;
      case "/download/":
        await download();
        break;
      case "/login/":
        login();
        break;
      case "/account/":
        await account();
        break;
      case "/orders/":
        await orders();
        break;
      case "/support/":
        await support();
        break;
      case "/desktop/authorize/":
        await authorize();
        break;
      case "/privacy/":
        legal("privacy");
        break;
      case "/terms/":
        legal("terms");
        break;
      default:
        title("页面不存在");
        main.innerHTML =
          '<div class="standalone-error"><h1>这个页面暂时找不到。</h1><p>链接可能已经变化，请返回首页继续。</p><a class="btn btn-primary" href="/">返回首页</a></div>';
    }
  }

  async function start() {
    try {
      const [config, me] = await Promise.all([
        api("/config"),
        api("/me").catch((error) => {
          if (error.status === 401) return null;
          throw error;
        }),
      ]);
      state.config = config;
      state.me = me;
      if (config.environment !== "production") {
        const banner = document.getElementById("environment-notice");
        banner.textContent =
          config.payments.wechat || config.payments.alipay
            ? "本地开发演示 · 当前非正式环境，请勿提交真实付款"
            : "本地开发演示 · 邮件默认保存在本机，模拟支付不产生扣款";
        banner.hidden = false;
      }
      updateNav();
      await render();
    } catch (error) {
      main.innerHTML = `<div class="standalone-error"><h1>暂时连接不上服务。</h1><p>${escape(error.message)}</p><button class="btn btn-primary" id="retry-page">重新加载</button></div>`;
      document.getElementById("retry-page").onclick = () => location.reload();
    }
  }

  document.getElementById("copyright-year").textContent =
    new Date().getFullYear();
  document.getElementById("nav-toggle").onclick = (event) => {
    const open = event.currentTarget.getAttribute("aria-expanded") !== "true";
    event.currentTarget.setAttribute("aria-expanded", String(open));
    event.currentTarget.setAttribute(
      "aria-label",
      open ? "收起导航" : "展开导航",
    );
    event.currentTarget.textContent = open ? "收起" : "菜单";
    document.getElementById("site-nav").classList.toggle("is-open", open);
  };
  window.addEventListener("pagehide", () => clearTimeout(state.poll));
  start();
})();
