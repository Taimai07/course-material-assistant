// Runs only after an explicit click, in the selected tab's isolated world.
function capturePage(selectionOnly) {
  const url = new URL(location.href);
  const hosts = ["classroom.google.com", "drive.google.com", "docs.google.com"];
  if (!hosts.includes(url.hostname)) throw new Error("请先打开 Google Classroom、Drive 或 Google 文档页面。");
  const selected = getSelection()?.toString().trim() || "";
  if (selectionOnly && !selected) throw new Error("请先在页面里选中文字。");
  const canonical = value => {
    const u = new URL(value, location.href);
    if (!["https:"].includes(u.protocol) || !hosts.includes(u.hostname)) return null;
    const id = u.searchParams.get("id");
    u.search = ""; u.hash = "";
    if (u.hostname === "drive.google.com" && id) return `https://drive.google.com/file/d/${encodeURIComponent(id)}/view`;
    return u.href;
  };
  const root = document.querySelector('[role="main"]') || document.querySelector("main") || document.body;
  const links = [...root.querySelectorAll("a[href]")].map(a => ({title: (a.innerText || a.getAttribute("aria-label") || "附件或来源").trim().slice(0,500), url: canonical(a.href)})).filter(x => x.url);
  const unique = [...new Map(links.map(x => [x.url,x])).values()].slice(0,1000);
  const text = (selectionOnly ? selected : root.innerText || "").trim();
  const canvas = url.hostname === "docs.google.com" && !selectionOnly;
  return {
    title: document.title.slice(0,500), source_url: canonical(location.href),
    text: text.slice(0,500000), links: unique,
    coverage: selectionOnly ? "selected_excerpt" : canvas ? "document_ui_only" : "rendered_partial",
    captured_at: new Date().toISOString(),
    warnings: [
      ...(canvas ? ["Google 文档可能用画布显示正文；此快照可能只有界面文字，全文请下载导入或使用官方 API。"] : []),
      ...(!selectionOnly ? ["仅包含当前已渲染的文字与链接；未展开、分页及虚拟滚动内容可能缺失。"] : []),
      ...(text.length > 500000 ? ["文字超出 500000 字符，已截断。"] : [])
    ]
  };
}
