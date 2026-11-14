/* fn-daen-m3u8 前端逻辑 */
(function () {
    "use strict";

    // CGI 入口：当前页面位于 .../index.cgi/index.html，推导出 API 前缀
    const CGI_BASE = location.pathname.split("/index.cgi")[0] + "/index.cgi/";

    const $ = (sel) => document.querySelector(sel);
    const urlsInput = $("#urls");
    const threadsInput = $("#threads");
    const dirSelect = $("#dirSelect");
    const folderName = $("#folderName");
    const startBtn = $("#startBtn");
    const formError = $("#formError");
    const groupList = $("#groupList");
    const emptyTip = $("#emptyTip");
    const taskCount = $("#taskCount");
    const refreshBtn = $("#refreshBtn");
    // 日志弹窗
    const logModal = $("#logModal");
    const logModalTitle = $("#logModalTitle");
    const logContent = $("#logContent");
    const logRefreshBtn = $("#logRefreshBtn");
    const logCopyBtn = $("#logCopyBtn");
    const logCloseBtn = $("#logCloseBtn");

    let tasks = [];
    let logTaskId = null; // 当前弹窗正在查看的任务 id
    let refreshTimer = null; // 任务列表自动刷新定时器

    // 任务组展开状态：默认展开；用户手动收起/展开后按 gid 记住
    const COLLAPSE_KEY = "fn-daen-m3u8.collapsedGroups";
    let collapsedGroups = new Set();
    try {
        const saved = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]");
        if (Array.isArray(saved)) collapsedGroups = new Set(saved);
    } catch (e) {
        collapsedGroups = new Set();
    }

    function isCollapsed(gid) {
        return collapsedGroups.has(gid);
    }

    function setCollapsed(gid, collapsed) {
        if (collapsed) collapsedGroups.add(gid);
        else collapsedGroups.delete(gid);
        try {
            localStorage.setItem(COLLAPSE_KEY, JSON.stringify(Array.from(collapsedGroups)));
        } catch (e) {
            /* localStorage 不可用时忽略，仅本次会话有效 */
        }
    }

    // 删除任务组需要点击两次确认（避免 iframe 沙箱下 confirm 被拦截导致无法删除）
    let pendingGroup = null; // { gid, timer }
    function pendingDeleteGid() {
        return pendingGroup ? pendingGroup.gid : null;
    }

    function armDeleteGroup(gid) {
        if (pendingGroup) clearTimeout(pendingGroup.timer);
        pendingGroup = {
            gid,
            timer: setTimeout(() => {
                pendingGroup = null;
                renderTasks();
            }, 5000),
        };
        renderTasks();
    }

    function disarmDeleteGroup() {
        if (pendingGroup) clearTimeout(pendingGroup.timer);
        pendingGroup = null;
    }

    // ===== 工具 =====
    // 解析一行输入：
    //   "名称$url"、"名称$url$ffm3u8"（$ 后的无用后缀会被丢弃）、或纯 "url"
    function parseLine(line) {
        const s = line.trim();
        if (!s) return null;
        const idx = s.indexOf("$");
        if (idx > 0) {
            const name = s.slice(0, idx).trim();
            let url = s.slice(idx + 1).trim();
            const idx2 = url.indexOf("$");
            if (idx2 >= 0) url = url.slice(0, idx2).trim(); // 丢弃 "$ffm3u8" 之类的无用后缀
            return { name, url };
        }
        return { name: "", url: s };
    }

    function isValidUrl(u) {
        return /^https?:\/\/[^\s"\\$`]+$/i.test(u);
    }

    function isValidName(n) {
        if (!n) return true;
        if (n.length > 200 || n === "." || n === "..") return false;
        return !/[/\\"$`*?:<>|]/.test(n);
    }

    function esc(s) {
        const div = document.createElement("div");
        div.textContent = s;
        return div.innerHTML;
    }

    function statusText(st) {
        return { waiting: "等待中", running: "下载中", done: "已完成", failed: "失败" }[st] || st;
    }

    // 单个任务的百分比：已完成按 100 计，失败按最后一次进度计，未开始为 0
    function taskPct(t) {
        if (t.status === "done") return 100;
        const p = t.progress ? Number(t.progress.pct) : 0;
        return isNaN(p) ? 0 : Math.min(100, Math.max(0, p));
    }

    // 任务组汇总：完成/失败/进行中数量与总进度
    function groupSummary(list) {
        let sum = 0;
        let done = 0;
        let failed = 0;
        let running = 0;
        let waiting = 0;
        list.forEach((t) => {
            sum += taskPct(t);
            if (t.status === "done") done += 1;
            else if (t.status === "failed") failed += 1;
            else if (t.status === "running") running += 1;
            else waiting += 1;
        });
        const pct = list.length ? Math.round((sum / list.length) * 10) / 10 : 0;
        return { pct, done, failed, running, waiting, total: list.length };
    }

    // 组状态徽标：任一下载中/等待中则显示进行中，全部结束再显示完成或失败
    function groupStatus(s) {
        if (s.running > 0) return "running";
        if (s.waiting > 0) return "waiting";
        if (s.failed > 0) return "failed";
        return "done";
    }

    function groupStatusText(s) {
        const parts = [];
        if (s.done) parts.push(s.done + " 完成");
        if (s.failed) parts.push(s.failed + " 失败");
        if (s.running) parts.push(s.running + " 下载中");
        if (s.waiting) parts.push(s.waiting + " 等待中");
        return parts.join(" · ");
    }

    // 组标题：优先展示组内第一个任务名/组创建时间
    function groupTitle(gid, list) {
        const first = list[0];
        if (first && first.started_at) return first.started_at.slice(5, 16); // MM-DD HH:MM
        return gid;
    }

    // ===== 渲染任务组 =====
    function groupTasks() {
        const map = new Map();
        tasks.forEach((t) => {
            const gid = t.gid || t.id;
            if (!map.has(gid)) map.set(gid, []);
            map.get(gid).push(t);
        });
        const groups = Array.from(map.entries()).map(([gid, list]) => {
            list.sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0));
            const newest = list.reduce((m, t) => Math.max(m, Number((t.id || "").split("_")[0]) || 0), 0);
            return { gid, list, newest };
        });
        // 新创建的任务组排最前
        groups.sort((a, b) => b.newest - a.newest);
        return groups;
    }

    function taskCard(t) {
        const card = document.createElement("div");
        card.className = "task-card";
        card.dataset.id = t.id;

        const head = document.createElement("div");
        head.className = "task-head";
        const pct = taskPct(t);
        head.innerHTML = `
            <span class="task-url">${esc(t.name || t.url)}</span>
            <span class="badge ${t.status}">${statusText(t.status)}</span>
            <span class="pct">${pct}%</span>
        `;

        const meta = document.createElement("div");
        meta.className = "task-meta";
        meta.innerHTML = `
            <span>任务ID：${esc(t.id)}</span>
            <span>线程数：${t.threads}</span>
            <span>开始：${esc(t.started_at || "-")}</span>
        `;
        if (t.name) {
            meta.innerHTML += `<span>保存名：${esc(t.name)}</span>`;
        }

        const bar = document.createElement("div");
        bar.className = "task-progress";
        bar.innerHTML = `
            <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
            <span class="pct-text">${pct}%${t.progress && t.progress.seg ? " (" + esc(t.progress.seg) + ")" : ""}</span>
        `;

        const actions = document.createElement("div");
        actions.className = "task-actions";

        const logBtn = document.createElement("button");
        logBtn.className = "btn ghost";
        logBtn.textContent = "查看日志";
        logBtn.onclick = () => openLog(t.id, t.name || t.url);
        actions.appendChild(logBtn);

        const copyBtn = document.createElement("button");
        copyBtn.className = "btn ghost";
        copyBtn.textContent = "复制日志";
        copyBtn.onclick = () => copyLog(t.id);
        actions.appendChild(copyBtn);

        const delBtn = document.createElement("button");
        delBtn.className = "btn ghost";
        delBtn.textContent = "移除任务";
        delBtn.onclick = () => deleteTask(t.id);
        actions.appendChild(delBtn);

        if (t.status === "running" || t.status === "waiting") {
            const stopBtn = document.createElement("button");
            stopBtn.className = "btn danger";
            stopBtn.textContent = "停止";
            stopBtn.onclick = async () => {
                stopBtn.disabled = true;
                stopBtn.textContent = "停止中…";
                await fetch(CGI_BASE + "?action=stop&id=" + encodeURIComponent(t.id), { method: "POST" });
                refreshTasks();
            };
            actions.appendChild(stopBtn);
        }

        card.appendChild(head);
        card.appendChild(meta);
        card.appendChild(bar);

        if (t.status === "done" && t.out_dir) {
            const out = document.createElement("div");
            out.className = "task-out";
            out.innerHTML = `保存位置：<code>${esc(t.out_dir)}</code>`;
            card.appendChild(out);
        } else if (t.status === "failed") {
            const out = document.createElement("div");
            out.className = "task-out";
            out.textContent = "下载失败，未生成输出文件，请查看日志确认原因（如流需要 ffmpeg 或地址已失效）。";
            card.appendChild(out);
        }

        card.appendChild(actions);
        return card;
    }

    function groupCard(g) {
        const s = groupSummary(g.list);
        const st = groupStatus(s);
        const collapsed = isCollapsed(g.gid);
        const pct = s.pct;

        const card = document.createElement("div");
        card.className = "group-card";
        card.dataset.gid = g.gid;

        const head = document.createElement("div");
        head.className = "group-head";

        const toggle = document.createElement("button");
        toggle.className = "group-toggle";
        toggle.title = collapsed ? "展开任务列表" : "收起任务列表";
        toggle.innerHTML = `<span class="caret">${collapsed ? "▸" : "▾"}</span>`;
        toggle.onclick = (e) => {
            e.stopPropagation();
            setCollapsed(g.gid, !isCollapsed(g.gid));
            renderTasks();
        };

        const title = document.createElement("div");
        title.className = "group-title";
        title.innerHTML = `
            <span class="group-name">任务组 ${esc(groupTitle(g.gid, g.list))}</span>
            <span class="badge ${st}">${statusText(st)}</span>
            <span class="group-stat">${esc(groupStatusText(s))}</span>
        `;
        title.onclick = () => {
            setCollapsed(g.gid, !isCollapsed(g.gid));
            renderTasks();
        };

        const acts = document.createElement("div");
        acts.className = "group-actions";

        const refreshOne = document.createElement("button");
        refreshOne.className = "btn ghost";
        refreshOne.textContent = "刷新";
        refreshOne.onclick = async () => {
            refreshOne.disabled = true;
            refreshOne.textContent = "刷新中…";
            await refreshTasks();
            refreshOne.disabled = false;
            refreshOne.textContent = "刷新";
            flashToast("已刷新");
        };
        acts.appendChild(refreshOne);

        const delGroup = document.createElement("button");
        delGroup.className = "btn danger";
        const armed = pendingDeleteGid() === g.gid;
        delGroup.textContent = armed ? "确认删除？" : "删除任务组";
        if (armed) delGroup.classList.add("armed");
        delGroup.onclick = (e) => {
            e.stopPropagation();
            if (pendingDeleteGid() === g.gid) {
                disarmDeleteGroup();
                deleteGroup(g.gid, g.list);
            } else {
                armDeleteGroup(g.gid);
            }
        };
        acts.appendChild(delGroup);

        head.appendChild(toggle);
        head.appendChild(title);
        head.appendChild(acts);

        const progress = document.createElement("div");
        progress.className = "group-progress";
        progress.innerHTML = `
            <div class="bar"><div class="bar-fill ${st}" style="width:${pct}%"></div></div>
            <span class="pct-text">${pct}%</span>
            <span class="group-count">${s.done + s.failed}/${s.total}</span>
        `;

        card.appendChild(head);
        card.appendChild(progress);

        if (!collapsed) {
            const body = document.createElement("div");
            body.className = "group-body";
            g.list.forEach((t) => body.appendChild(taskCard(t)));
            card.appendChild(body);
        }

        return card;
    }

    function renderTasks() {
        groupList.innerHTML = "";
        const running = tasks.filter((t) => t.status === "running").length;
        const groups = groupTasks();
        taskCount.textContent = tasks.length
            ? `(${groups.length} 个任务组，${tasks.length} 个任务，${running} 个下载中)`
            : "";
        emptyTip.style.display = tasks.length ? "none" : "block";

        // 清理已不存在的任务组的折叠记录
        const alive = new Set(groups.map((g) => g.gid));
        let changed = false;
        collapsedGroups.forEach((gid) => {
            if (!alive.has(gid)) {
                collapsedGroups.delete(gid);
                changed = true;
            }
        });
        if (changed) {
            try {
                localStorage.setItem(COLLAPSE_KEY, JSON.stringify(Array.from(collapsedGroups)));
            } catch (e) {
                /* 忽略 */
            }
        }

        groups.forEach((g) => groupList.appendChild(groupCard(g)));
    }

    // ===== 日志弹窗 =====
    async function loadLogIntoModal() {
        if (!logTaskId) return;
        try {
            const res = await fetch(CGI_BASE + "?action=log&id=" + encodeURIComponent(logTaskId));
            const data = await res.json();
            logContent.textContent = data.log || "(暂无日志)";
            logContent.scrollTop = logContent.scrollHeight;
        } catch (e) {
            logContent.textContent = "日志加载失败：" + e.message;
        }
    }

    async function openLog(id, title) {
        logTaskId = id;
        logModalTitle.textContent = "任务日志" + (title ? "：" + title : "");
        logContent.textContent = "加载中…";
        logModal.hidden = false;
        await loadLogIntoModal();
    }

    function closeLog() {
        logModal.hidden = true;
        logTaskId = null;
        logContent.textContent = "";
    }

    async function copyLog(id) {
        try {
            const res = await fetch(CGI_BASE + "?action=log&id=" + encodeURIComponent(id));
            const data = await res.json();
            const text = data.log || "";
            try {
                await navigator.clipboard.writeText(text);
            } catch (e) {
                // 降级方案：隐藏 textarea 复制
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                document.execCommand("copy");
                ta.remove();
            }
            flashToast("日志已复制");
        } catch (e) {
            flashToast("复制失败：" + e.message);
        }
    }

    // ===== 删除单个任务 =====
    async function deleteTask(id) {
        try {
            const res = await fetch(CGI_BASE + "?action=delete_log&id=" + encodeURIComponent(id), { method: "POST" });
            const data = await res.json();
            if (data.ok) {
                if (logTaskId === id) closeLog();
                flashToast("任务已移除");
                refreshTasks(); // 删除成功后自动刷新任务列表
            } else {
                flashToast("删除失败：" + (data.error || "未知错误"));
            }
        } catch (e) {
            flashToast("删除失败：" + e.message);
        }
    }

    // ===== 删除整个任务组：组内任务全部终止并移除 =====
    // 二次确认由按钮的两段式点击完成，此处不再使用 confirm（fnOS 的 iframe 沙箱可能拦截）
    async function deleteGroup(gid, list) {
        closeLogIfInGroup(gid, list);
        try {
            const res = await fetch(CGI_BASE + "?action=delete_group&gid=" + encodeURIComponent(gid), { method: "POST" });
            const data = await res.json();
            if (data.ok) {
                setCollapsed(gid, false);
                flashToast("任务组已删除（" + (data.removed || 0) + " 个任务）");
                refreshTasks();
            } else {
                flashToast("删除失败：" + (data.error || "未知错误"));
            }
        } catch (e) {
            flashToast("删除失败：" + e.message);
        }
    }

    // 若当前日志弹窗里的任务属于被删除的组，则关闭弹窗
    function closeLogIfInGroup(gid, list) {
        if (!logTaskId) return;
        if (logTaskId === gid || list.some((t) => t.id === logTaskId)) closeLog();
    }

    // ===== 轻提示 =====
    let toastTimer = null;
    function flashToast(msg) {
        let el = document.getElementById("toast");
        if (!el) {
            el = document.createElement("div");
            el.id = "toast";
            el.className = "toast";
            document.body.appendChild(el);
        }
        el.textContent = msg;
        el.classList.add("visible");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove("visible"), 1800);
    }

    // ===== 刷新状态（手动） =====
    async function refreshTasks() {
        try {
            const res = await fetch(CGI_BASE + "?action=status", { cache: "no-store" });
            const data = await res.json();
            tasks = data.tasks || [];
            renderTasks();
        } catch (e) {
            // 网络或 CGI 异常时保留现有列表
        }
    }

    // ===== 加载可用目录 & 默认配置 =====
    async function loadDirs() {
        try {
            const res = await fetch(CGI_BASE + "?action=dirs", { cache: "no-store" });
            const data = await res.json();
            const dirs = data.dirs || [];
            if (data.default_threads) {
                threadsInput.value = data.default_threads;
            }
            // 刷新间隔配置（M3U8_REFRESH_SECOND）
            if (data.refresh_seconds) {
                const sec = parseInt(data.refresh_seconds, 10);
                if (sec >= 1 && sec <= 300) {
                    clearInterval(refreshTimer);
                    refreshTimer = setInterval(refreshTasks, sec * 1000);
                }
            }
            dirSelect.innerHTML = "";
            if (!dirs.length) {
                const opt = document.createElement("option");
                opt.value = "";
                opt.textContent = "（无可用目录）";
                dirSelect.appendChild(opt);
                dirSelect.disabled = true;
            } else {
                dirs.forEach((d, i) => {
                    const opt = document.createElement("option");
                    opt.value = d;
                    opt.textContent = d;
                    if (i === 0) opt.selected = true;
                    dirSelect.appendChild(opt);
                });
                dirSelect.disabled = false;
            }
        } catch (e) {
            dirSelect.innerHTML = '<option value="">（无可用目录）</option>';
            dirSelect.disabled = true;
        }
    }

    // ===== 提交下载 =====
    async function submit() {
        formError.textContent = "";
        const items = urlsInput.value.split("\n").map(parseLine).filter((x) => x !== null);
        if (!items.length) {
            formError.textContent = "请输入至少一个 m3u8 地址";
            return;
        }
        for (const it of items) {
            if (!isValidUrl(it.url)) {
                formError.textContent = "存在非法地址：" + it.url;
                return;
            }
            if (!isValidName(it.name)) {
                formError.textContent = "保存名含非法字符：" + it.name;
                return;
            }
        }
        let threads = parseInt(threadsInput.value, 10);
        if (isNaN(threads) || threads < 1) threads = 20;
        if (threads > 64) threads = 64;

        const dir = dirSelect.value || "";
        const folder = folderName.value.trim();
        if (folder.length > 100 || folder === "." || folder === ".." || /[/\\"$`*?:<>|]/.test(folder)) {
            formError.textContent = "文件夹名包含非法字符（仅允许字母、数字、中文、下划线、短横线、空格）";
            return;
        }

        startBtn.disabled = true;
        startBtn.textContent = "正在创建任务…";
        try {
            const params = new URLSearchParams();
            params.set("threads", String(threads));
            if (dir) params.set("dir", dir);
            if (folder) params.set("folder", folder);
            items.forEach((it) => {
                params.append("url", it.name ? it.name + "$" + it.url : it.url);
            });
            const res = await fetch(CGI_BASE + "?action=download", {
                method: "POST",
                body: params,
            });
            const data = await res.json();
            if (!data.ok) {
                formError.textContent = data.error || "创建任务失败";
                return;
            }
            urlsInput.value = "";
            // 新建的任务组默认展开
            if (data.gid) setCollapsed(data.gid, false);
            await refreshTasks(); // 新任务在最前
            flashToast("已创建 " + (data.tasks || []).length + " 个下载任务");
        } catch (e) {
            formError.textContent = "请求失败：" + e.message;
        } finally {
            startBtn.disabled = false;
            startBtn.textContent = "开始下载";
        }
    }

    // ===== 初始化 =====
    startBtn.addEventListener("click", submit);
    refreshBtn.addEventListener("click", refreshTasks);
    logCloseBtn.addEventListener("click", closeLog);
    logRefreshBtn.addEventListener("click", loadLogIntoModal);
    logCopyBtn.addEventListener("click", () => logTaskId && copyLog(logTaskId));
    urlsInput.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") submit();
    });
    logModal.addEventListener("click", (e) => {
        if (e.target === logModal) closeLog(); // 点击遮罩关闭
    });

    loadDirs();
    refreshTasks();
    // 任务列表自动刷新（间隔由 M3U8_REFRESH_SECOND 配置，loadDirs 时动态调整；日志弹窗独立不受影响）
    refreshTimer = setInterval(refreshTasks, 2000);
})();
