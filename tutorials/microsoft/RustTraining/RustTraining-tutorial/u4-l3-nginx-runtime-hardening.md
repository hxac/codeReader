# nginx 运行时与安全加固

## 1. 本讲目标

上一讲（u4-l2）我们跟着 Dockerfile 走完了两阶段构建：builder 阶段产出 `site/`，runtime 阶段把它交给一个 nginx 容器。当时 runtime 阶段被当作黑盒——「一个非特权 nginx 在 8080 端口提供静态文件」。

本讲打开这个黑盒。学完后你应该能够：

1. 逐段解释 `try_files $uri $uri/ $uri.html =404;` 的含义，并说明它为什么是对齐 GitHub Pages 无扩展名链接行为的关键一行。
2. 准确说出 nginx `add_header` 指令的继承规则，解释配置注释里「add_header 不被继承」的坑如何迫使每个 `location` 重复声明安全响应头。
3. 对比 HTML 页面与静态资产的两套缓存策略，说明为什么这里不能用 `immutable`；并解释 gzip 各参数的作用。
4. 解释非特权镜像（uid 101 + 8080 端口）与 `no-new-privileges` 各自堵住了什么攻击面。

本讲是部署篇第三节，也是整个「基础设施层」源码精读的最后一讲。

## 2. 前置知识

本讲会用到下面几个概念，先用通俗语言过一遍。

### 2.1 nginx 与 location 块

nginx 是一个高性能的静态文件 Web 服务器（也能做反向代理），行为完全由配置文件驱动。它的核心抽象是 `server` 块（一个监听端口 + 一组规则）和 `location` 块（按请求 URI 匹配的子规则）。匹配顺序大致是：正则 `location`（`~` 大小写敏感、`~*` 大小写不敏感）优先于普通前缀 `location`，所以 `location ~* \.(css|js)$` 会抢在 `location /` 之前接走所有样式和脚本请求。

### 2.2 HTTP 响应头

你在 u2-l6 已经手写过 HTTP/1.1 响应报文：状态行、CRLF 分隔的头部、空行、正文。当时我们手工构造了 `Content-Type` 和 `Content-Length`。本讲的 `add_header` 与 `expires` 做的事情本质相同——往响应头部里追加键值对——只是由 nginx 代劳，并且会作用于所有响应，而不只是单个文件。

### 2.3 缓存的新鲜度与内容哈希

浏览器缓存的核心问题是「缓存的副本还能不能直接用」：

- `Cache-Control: no-cache`：可以存，但每次使用前必须向服务器重新验证（配合 `ETag` 可以拿到无正文的 304 响应）。注意它**不是**「不缓存」——那是 `no-store`。
- `Cache-Control: max-age=86400`：从现在起 86400 秒（1 天）内直接用本地副本，不询问服务器。
- `immutable`：告诉浏览器即使刷新页面也不要重新验证。它只对 URL 含内容哈希的资产（如 `app.a1b2c3.js`，内容变则文件名变）是安全的——URL 不变就意味着字节不变。

本仓库的一个关键约束是：**mdBook 生成的资产（book.js、ace.js、css）URL 不含内容哈希**，rebuild 之后同一个 URL 会提供新字节。这直接决定了后面的缓存策略。

### 2.4 特权端口与容器中的 root

Linux 规定绑定 1024 以下的端口（如 80、443）需要 `CAP_NET_BIND_SERVICE` 能力，默认只有 root 有。8080 大于 1024，普通用户即可绑定。另外要记住：默认配置下（没有 user namespace 重映射时），容器内的 root 在宿主机内核眼里就是 uid 0——容器内提权到 root，离宿主机 root 就只隔一层内核攻击面。所以「容器里也尽量不用 root」是纵深防御的基本功。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `docker/nginx.conf` | runtime 阶段被复制进镜像的 nginx 配置，本讲主战场：路由、安全头、缓存、gzip、日志全在这 46 行里 |
| `docker/compose.yaml` | 自托管编排：构建上下文、端口映射、重启策略、`no-new-privileges`、健康检查 |
| `docker/Dockerfile` | 只看 runtime 阶段（L80 起）：基镜像选择、配置与产物的装配、`EXPOSE` 与 `HEALTHCHECK`（builder 阶段已在 u4-l2 精读） |
| `docker/README.md` | 自托管部署说明，Notes 一节记录了已做的加固与有意未启用的 `read_only` |
| `.github/workflows/docker.yml` | CI 冒烟测试，其中无扩展名链接断言直接守护本讲的 `try_files` 行为（u4-l2 已讲，这里只取呼应点） |
| `xtask/src/main.rs` | 对比参照：本地开发服务器对目录请求的处理，用来反衬 nginx 需要额外处理什么 |

## 4. 核心概念与源码讲解

### 4.1 try_files：无扩展名链接的兼容层

#### 4.1.1 概念说明

这个模块解决的问题是：**书里的链接有时不带 `.html` 扩展名，而 nginx 默认不会帮你补**。

mdBook 确实会为每章生成真实的 `.html` 文件（例如 `ch00-introduction.md` 编译成 `ch00-introduction.html`），`SUMMARY.md` 生成的侧边栏链接也是带扩展名的。但仓库作者在手写正文链接、或在 Markdown 里引用其他章节时，偶尔会省略扩展名。GitHub Pages 的静态服务器对这种无扩展名路径会自动尝试补上 `.html` 再解析，所以这些链接在 Pages 上一直能工作。

一旦把站点搬进自己的 nginx 容器（u4-l2 的自托管路线），这层「补扩展名」的宽容就没有了——nginx 默认按字面路径找文件，`/async-book/ch00-introduction` 找不到就 404。于是仓库需要在 nginx 侧显式重建这层兼容。承担这个职责的是 `try_files` 指令：它接受一串候选，按顺序逐个尝试，全部失败才用最后一个兜底参数。

#### 4.1.2 核心流程

`try_files $uri $uri/ $uri.html =404;` 的解析过程可以写成这样的伪代码：

```text
resolve(请求 URI):
    # 候选一：$uri —— 把 URI 当作字面文件路径，在 root 下查找
    if 存在普通文件(root + URI):
        返回该文件内容, 200

    # 候选二：$uri/ —— 把 URI 当作目录
    if 存在目录(root + URI):
        301 重定向到带尾斜杠的形式（若请求未带斜杠）
        随后由 index 指令在该目录下定位 index.html

    # 候选三：$uri.html —— 在原 URI 后拼上 ".html" 再当文件查找
    if 存在普通文件(root + URI + ".html"):
        返回该文件内容, 200

    # 兜底：=404 —— 所有候选失败
    返回 404
```

三个候选各管一类请求：

| 请求示例 | 命中的候选 | 结果 |
| --- | --- | --- |
| `/async-book/ch00-introduction.html` | `$uri` | 200，直接返回文件 |
| `/async-book/`（书的目录页） | `$uri` 失败（是目录不是文件）→ `$uri/` | 定位到 `async-book/index.html`，200 |
| `/async-book/ch00-introduction`（无扩展名） | `$uri` 失败 → `$uri/` 失败（不是目录）→ `$uri.html` | 200，补全扩展名后返回 |
| `/async-book/no-such-page` | 三个候选全部失败 | 404 |

其中候选二里的「301 补尾斜杠」值得专门对照：你在 u2-l5 见过 xtask 内置服务器完全相同的设计——浏览器按相对路径解析 `index.html` 里的 `./ch01.html` 这类链接时，URL 有没有尾斜杠会导致解析到不同的基路径，所以服务器必须先把 `/async-book` 规范成 `/async-book/`。

#### 4.1.3 源码精读

先看 server 块的骨架，确定监听端口与文件根：

[docker/nginx.conf:L1-L6](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L1-L6) —— 监听 8080（`server_name _` 表示不挑 Host 头，来者不拒）；`root` 指定静态文件根，正是 u4-l2 里 `COPY --from=builder` 落盘 site/ 产物的目标路径 `/usr/share/nginx/html`；`index index.html` 供 `$uri/` 候选在目录内定位入口文件。

然后是本模块的主角：

[docker/nginx.conf:L8-L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L8-L14) —— 配置作者用注释记录了两件事：一是 try_files 存在的理由（「mdbook 生成真实 .html 文件，但内部和手写链接有时省略扩展名，两者都解析才能与 GitHub Pages 行为保持一致」），二是 `add_header` 不被继承的坑——这条留给 4.2 精读。

[docker/nginx.conf:L15-L22](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L15-L22) —— `location /` 是兜底前缀匹配（所有未被正则 location 抢走的请求都到这里），L16 的 try_files 就是 4.1.2 伪代码的原型；L18-L21 的四个 `add_header` 属于 4.2 的内容。

这行配置不是孤立的——CI 冒烟测试里有一条断言专门守护它的行为：

[.github/workflows/docker.yml:L57-L60](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/.github/workflows/docker.yml#L57-L60) —— CI 起容器后依次断言三件事：落地页 title 存在、`/async-book/` 返回 200（守护候选二）、`/async-book/ch00-introduction`（无扩展名）返回 200（守护候选三）。如果有人改坏 try_files，这条流水线会变红。

最后做个三方对比，看清「兼容层」补在哪。回看 u2-l5 精读过的 xtask 解析函数：

[xtask/src/main.rs:L356-L362](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/xtask/src/main.rs#L356-L362) —— 本地开发服务器只处理「URI 是目录」这一种情况：缺尾斜杠则 301，然后 `push("index.html")`。它**没有** `.html` 扩展名补全逻辑——`/async-book/ch00-introduction` 在 `cargo xtask serve` 下会 404。这不是缺陷，而是「本地预览够用」哲学（u2-l6 总结过）的又一体现：开发时书内链接由 mdBook 生成、总是规范的；无扩展名链接的问题只在「手写链接 + 生产服务器」的组合下暴露，所以只有面向生产的 nginx 需要这层兼容。

| 请求 | GitHub Pages | nginx（本配置） | `cargo xtask serve` |
| --- | --- | --- | --- |
| `/async-book/` | 200 | 200（候选二） | 200（目录 → index.html） |
| `/async-book/ch00-introduction` | 200（自动补 .html） | 200（候选三） | **404** |
| `/async-book`（无尾斜杠） | 301 → `/async-book/` | 301 → `/async-book/` | 301 → `/async-book/` |

#### 4.1.4 代码实践

> 本实践需要本地 Docker 环境。下列命令与预期现象**待本地验证**，不要假设已经运行过。

1. **实践目标**：亲眼验证 try_files 三个候选各自的行为，并确认删掉 `$uri.html` 后无扩展名链接会 404。

2. **操作步骤**：

   ```bash
   # 在仓库根目录构建并启动（构建上下文必须是仓库根，原因见 u4-l2）
   docker build -f docker/Dockerfile -t rust-training:local .
   docker run --rm -d --name rt-nginx -p 3000:8080 rust-training:local

   # 三个探测：文件、目录、无扩展名
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/async-book/index.html
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/async-book/
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/async-book/ch00-introduction

   # 破坏性对照：删掉 $uri.html 候选后重建
   # 把 nginx.conf L16 改为: try_files $uri $uri/ =404;
   docker build -f docker/Dockerfile -t rust-training:local .
   docker rm -f rt-nginx && docker run --rm -d --name rt-nginx -p 3000:8080 rust-training:local
   curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/async-book/ch00-introduction
   ```

   改完后记得把 nginx.conf 还原（`git checkout -- docker/nginx.conf`）。

3. **需要观察的现象**：第三条 curl（无扩展名链接）的状态码在改动前后从 200 变为 404，而前两条始终是 200。

4. **预期结果**：改动前三个请求依次为 `200 / 200 / 200`；删掉 `$uri.html` 后变为 `200 / 200 / 404`——同时可以推断 `.github/workflows/docker.yml` L60 的断言会失败，CI 变红。

5. 如果本地没有 Docker，退化为源码阅读型实践：对照 4.1.2 的表格，在 `site/`（或本地 `cargo xtask build` 产出的目录）里用 `ls` 确认 `async-book/ch00-introduction.html` 存在而 `ch00-introduction`（无扩展名）不存在，从而论证候选三为什么必要。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `$uri/` 从 try_files 里删掉，访问 `http://localhost:3000/async-book/` 会发生什么？哪条 CI 断言会抓到？

**答案**：带尾斜杠的请求会让 `$uri` 候选去查找名为 `async-book/` 的普通文件——它是目录，不是文件，查找失败；没有 `$uri/` 候选后 nginx 直接落入 `=404`。`.github/workflows/docker.yml` L58 断言 `/async-book/` 必须返回 200，会在 CI 上失败。这也解释了为什么 try_files 的候选顺序是「文件 → 目录 → 补扩展名」：最便宜的精确匹配放最前面。

**练习 2**：try_files 的最后一个参数（这里是 `=404`）能省略吗？

**答案**：不能。nginx 规定 try_files 的最后一个参数是「全部候选失败后的兜底」，要么是一个 URI（内部重定向，如转到错误页），要么是 `=code` 形式的状态码。省略它是一个配置语法错误，nginx 会拒绝加载整个配置文件、容器直接起不来。

**练习 3**：xtask 的开发服务器为什么可以不做 `.html` 补全，而 nginx 必须做？

**答案**：两者的消费者不同。`cargo xtask serve` 服务于「作者正在写书」的场景，页面内导航由 mdBook 从 SUMMARY.md 生成、链接规范；而 nginx 容器面向真实读者，必须兼容仓库里累积下来的手写无扩展名链接，并且要对齐 GitHub Pages 已有的行为——否则同一份内容在两条部署路线上表现为「有的链接能点、有的不能」。

### 4.2 安全响应头与 add_header 继承陷阱

#### 4.2.1 概念说明

这个模块解决两个问题：**发哪些安全响应头**，以及一个反直觉的配置陷阱——**这些头为什么要在每个 location 里重复写**。

先看头本身。`location /` 发出四个头，其中三个是安全头：

| 响应头 | 取值 | 防御的攻击 |
| --- | --- | --- |
| `X-Content-Type-Options` | `nosniff` | 禁止浏览器 MIME 嗅探。没有它，浏览器可能「自作主张」把声明为文本的内容当脚本执行；有了它，`Content-Type` 说什么就是什么 |
| `X-Frame-Options` | `SAMEORIGIN` | 防点击劫持：禁止其他站点用 iframe 套住本站、诱导用户点击 |
| `Referrer-Policy` | `no-referrer` | 跨站跳转时不携带 Referrer，减少读者访问路径的泄露 |
| `Cache-Control` | `no-cache` | 不是安全头，是缓存策略，4.3 精读 |

然后是陷阱。直觉上，nginx 的指令应该「从外往内继承」：在 `server` 级写的配置，`location` 级应该自动生效。大多数指令确实如此，但 `add_header` 的继承规则是文档化的特例：

> `add_header` 只有在**当前层级一条 add_header 都没写**时，才从上一层级继承。只要当前层级出现了任何一条 `add_header`，上一层的全部 `add_header` 一次性失效。

本配置的两个 location 都有自己的 `add_header`（`location /` 要发 `Cache-Control`，静态资源 location 配合 `expires` 也不得不发自己的头），所以哪怕把安全头提到 server 级，也会被两个 location 同时「顶掉」。作者的解法是干脆放弃 server 级声明，让每个 location 显式重复列出自己需要的头，并用注释把这个决定记录在案——防止后来的维护者「顺手优化」把头提到 server 级，然后困惑为什么全部失效。

#### 4.2.2 核心流程

规则可以用一个判定描述：

```text
effective_headers(location L):
    if L 内声明了 ≥1 条 add_header:
        返回 L 自己声明的那些（server 级的全部作废）
    else:
        返回 server 级声明的那些（若有）
```

推论：本配置中任何一条想在「所有响应」上都出现的新头，都必须**同时**加进两个 location，漏一处就会出现「HTML 页面有、CSS 没有」的不一致——这正是综合实践要动手验证的事。

另一个关键字是 `always`。`add_header` 默认只在「成功类」状态码（200、201、204、206、301、302、303、304、307、308）上发送；加上 `always` 后，404、5xx 等所有响应都会带上。对安全头来说这不可省略：404 页面同样是 HTML，同样可能被嗅探或被 iframe 嵌套。

#### 4.2.3 源码精读

配置作者留下的「事故预防注释」：

[docker/nginx.conf:L12-L14](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L12-L14) —— 注释原文说明：add_header 不会被继承进「声明了自己 add_header」的块，所以每个 location 重复列出所需的头，而不是依赖 server 级声明。这是全配置最重要的一行注释，它解释了下面两段代码「看起来冗余」的写法。

`location /` 的四个头，全部带 `always`：

[docker/nginx.conf:L18-L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L18-L21) —— HTML 页面响应携带：`Cache-Control: no-cache`（缓存策略，见 4.3）加三个安全头。由于 4.2.1 的继承规则，这四行必须写在这里，而不是 server 级。

静态资源 location 只重复三个安全头：

[docker/nginx.conf:L31-L33](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L31-L33) —— 与 `location /` 相比少了 `Cache-Control: no-cache`。原因不是遗漏，而是冲突：这个 location 用的是 `expires 1d` 指令（L29），而 `expires` 指令自己会生成 `Cache-Control: max-age=86400`；若在这里再 `add_header Cache-Control "no-cache"`，同一个响应会出现两条语义相反的 `Cache-Control` 头，浏览器行为未定义。三个安全头则与 `expires` 互不干扰，必须原样重复。

顺带注意正则匹配的写法：

[docker/nginx.conf:L28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L28) —— `~*` 表示大小写不敏感的正则 location，匹配所有以这些扩展名结尾的请求（css、js、woff/woff2、ttf、svg、png、jpg、gif、ico）。nginx 的匹配优先级里正则 location 先于普通前缀 location，所以这些请求会绕过 `location /` 的 try_files 与四个头，进入本块的缓存策略。

#### 4.2.4 代码实践

> 本实践需要本地 Docker 环境，命令与现象**待本地验证**。

1. **实践目标**：亲手复现「add_header 不被继承」——证明把安全头提到 server 级在本配置里行不通。

2. **操作步骤**：

   ```bash
   # 基线：确认当前配置下 CSS 响应带 X-Content-Type-Options
   docker run --rm -d --name rt-nginx -p 3000:8080 rust-training:local
   curl -sI http://localhost:3000/ | grep -i x-content-type-options

   # 实验：把三个安全头从两个 location 剪切到 server 块（L3 之后），
   # 即只在 server 级声明一次，location 内的对应行删除
   # 编辑 docker/nginx.conf 后重建：
   docker build -f docker/Dockerfile -t rust-training:local .
   docker rm -f rt-nginx && docker run --rm -d --name rt-nginx -p 3000:8080 rust-training:local

   # 再次探测首页与任意 css：
   curl -sI http://localhost:3000/ | grep -i x-content-type-options
   curl -sI http://localhost:3000/async-book/css/  # 或先从首页 HTML 里找一个真实 css 路径
   ```

   做完务必 `git checkout -- docker/nginx.conf` 还原。

3. **需要观察的现象**：头提到 server 级之后，首页与 css 响应里的 `X-Content-Type-Options` 都消失了（两个 location 各自有 `add_header`，server 级被顶掉）。

4. **预期结果**：基线探测输出 `X-Content-Type-Options: nosniff`；实验后输出为空。这与 L12-L14 注释的警告完全一致。

5. 进一步验证 `always` 的作用：请求一个不存在的路径 `curl -sI http://localhost:3000/no-such-page`，确认 404 响应里仍有三个安全头——如果去掉 `always`，这些头只会在 2xx/3xx 上出现。

#### 4.2.5 小练习与答案

**练习 1**：为什么静态资源 location 不像 `location /` 那样声明 `Cache-Control: no-cache`？

**答案**：因为它已经用 `expires 1d` 设定了自己的缓存策略，而 `expires` 指令会生成 `Cache-Control: max-age=86400`。再叠加一条 `Cache-Control: no-cache` 会产生两条语义冲突的同名头，浏览器对重复 `Cache-Control` 的处理不保证合并、行为未定义。正确做法就是本配置的做法：两个 location 各自持有一套自洽的缓存头。

**练习 2**：如果把所有 `add_header` 的 `always` 参数删掉，哪个响应会失去安全头？为什么这算安全问题？

**答案**：所有 4xx/5xx 响应（典型如 try_files 兜底的 404 页面）将不再携带安全头。404 页面同样是服务器返回的 HTML 内容，同样可能被浏览器嗅探（`nosniff` 缺失）或被第三方 iframe 嵌套（`X-Frame-Options` 缺失）。安全头的意义在于覆盖「所有出口」，只保护 200 响应等于给攻击者留下了从错误页发起的利用面。

**练习 3**：假设要新增一个只在 HTML 页面需要、css 不需要的头（比如 `Content-Security-Policy`），应该加在哪几处？

**答案**：只加 `location /` 一处。CSP 通过 `<meta>` 或响应头注入页面，静态资产响应携带它没有意义。反过来，如果要一个全站头（如 `Strict-Transport-Security`），则两个 location 都要加——这就是继承规则决定的维护成本。

### 4.3 缓存与 gzip：两种资产、两套策略

#### 4.3.1 概念说明

这个模块回答两个传输层问题：**浏览器能缓存多久**，以及**字节如何在网络上压小**。

缓存的核心矛盾是：站点内容 bake 在镜像里（u4-l2），rebuild 镜像后**同一个 URL 会提供新字节**——因为 mdBook 的资产 URL 不含内容哈希（对比现代前端构建的 `app.a1b2c3.js`：内容变则文件名变，才可以放心 `immutable`）。于是：

- **HTML 页面**是读者最先拿到的入口，必须保证 rebuild 后立即可见 → `Cache-Control: no-cache`：允许缓存，但每次使用前重新验证（可命中 304 空正文，代价很小）。
- **静态资产**（book.js、ace.js、css、字体、图片）体积大、变更少 → `expires 1d`：允许缓存一整天，换取回访零流量。一天后浏览器会重新验证，所以最坏情况下读者用「昨天的 JS」配「今天的 HTML」的窗口被限制在 24 小时——这正是注释里说的「保持便宜，又不把旧 JS 钉死在从不重新验证的浏览器里」。

gzip 则是另一件事：nginx 在网络上传输前把文本压小。压缩的是**传输字节**，不影响磁盘文件和浏览器解析。

#### 4.3.2 核心流程

两个 location 的缓存判定：

```text
if 请求命中正则 location（css/js/字体/图片）:
    响应头: Expires: <1 天后的 HTTP 日期>
            Cache-Control: max-age=86400      # 由 expires 1d 生成
else（HTML 页面、无扩展名页面）:
    响应头: Cache-Control: no-cache            # 每次 revalidate，可 304
```

gzip 的决策链：

```text
if 响应体长度 ≥ 1024            # gzip_min_length：太小的压了反而更大
and Content-Type ∈ gzip_types   # 默认只压 text/html，列表是追加
and 客户端发送了 Accept-Encoding: gzip:
    压缩响应体，加 Content-Encoding: gzip
    加 Vary: Accept-Encoding    # gzip_vary：告诉中间缓存按该头区分变体
```

#### 4.3.3 源码精读

HTML 页面的即时更新策略：

[docker/nginx.conf:L18](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L18) —— `no-cache` 的准确语义是「可缓存但使用前必须向源服务器验证」，配合 mdBook 输出的 `ETag`/`Last-Modified`，验证命中时服务器只回一个无正文的 304，浏览器继续用本地副本——既不牺牲新鲜度，也不浪费流量。

静态资产的折中策略及其理由，注释写得很完整：

[docker/nginx.conf:L24-L29](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L24-L29) —— 注释先点破前提「mdbook 的资产不含内容哈希——同一 URL 在 rebuild 后提供新字节——所以不能标记 immutable」，随后给出结论：短过期（1 天）让缓存「便宜」而不把旧 JS 钉死。L29 的 `expires 1d` 会同时生成 `Expires` 与 `Cache-Control: max-age=86400` 两个头。

gzip 全家桶：

[docker/nginx.conf:L36-L41](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L36-L41) —— 六个参数各自的作用：

| 参数 | 作用 |
| --- | --- |
| `gzip on` | 总开关 |
| `gzip_vary on` | 响应加 `Vary: Accept-Encoding`，防止共享缓存把压缩版发给不支持 gzip 的客户端 |
| `gzip_min_length 1024` | 小于 1KB 不压——gzip 头开销可能超过压缩收益，小文件压完反而更大 |
| `gzip_proxied any` | 对经过代理的请求也压缩（默认 off）；站点挂在内网反代后面时保证压缩不失效 |
| `gzip_types ...` | 追加压缩类型。nginx 默认**只**压 `text/html`，css/js/json/svg/字体都要显式列出才会压 |

日志出口：

[docker/nginx.conf:L43-L44](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/nginx.conf#L43-L44) —— 访问日志与错误日志分别导向容器的 stdout/stderr，这是容器的十二要素惯例：日志交给 `docker logs` / 日志驱动收集，容器内不落盘（也为 4.4 提到的 `read_only` 留了可能性）。

#### 4.3.4 代码实践

> 本实践需要本地 Docker 环境，命令与现象**待本地验证**。

1. **实践目标**：验证两种资产的缓存头差异与 gzip 是否真的生效。

2. **操作步骤**：

   ```bash
   docker run --rm -d --name rt-nginx -p 3000:8080 rust-training:local

   # 从首页 HTML 里找一个真实 css 路径，例如：
   #   grep -o 'href="[^"]*\.css"' <(curl -s http://localhost:3000/async-book/)
   # 然后对比两种响应的缓存头：
   curl -sI http://localhost:3000/async-book/ | grep -iE 'cache-control|expires'
   curl -sI http://localhost:3000/<上一步找到的.css路径> | grep -iE 'cache-control|expires'

   # gzip：带与不带 Accept-Encoding 各请求一次 css
   curl -sI -H 'Accept-Encoding: gzip' http://localhost:3000/<css路径> \
     | grep -iE 'content-encoding|vary|content-length'
   curl -sI -H 'Accept-Encoding: identity' http://localhost:3000/<css路径> \
     | grep -iE 'content-encoding|vary|content-length'
   ```

3. **需要观察的现象**：HTML 响应只有 `Cache-Control: no-cache`，css 响应有 `Cache-Control: max-age=86400` 和 `Expires`；带 `Accept-Encoding: gzip` 的请求多出 `Content-Encoding: gzip` 与 `Vary: Accept-Encoding`，且 `Content-Length` 显著小于未压缩时。

4. **预期结果**：与 4.3.2 的判定一致。另可找一个小于 1KB 的文件（如 `favicon.ico` 若小于阈值）验证 `gzip_min_length`：即使带了 Accept-Encoding 也不会出现 `Content-Encoding: gzip`。

5. 若无本地 Docker，退化为阅读型实践：对照 L36-L41 的 gzip_types 列表，回答「`.woff2` 字体会被压缩吗？该不该在列表里？」——见下面练习 2。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `location /` 的缓存头改成 `Cache-Control: no-store`，读者体验和流量会有什么变化？

**答案**：`no-store` 是「完全不存」：每次访问页面都要完整下载 HTML 正文，连 304 协商都没有。本站 HTML 页面本身不大（几十 KB），损失主要是不必要的重复流量与首屏延迟；而 `no-cache` 的语义已经足够强（保证 rebuild 立即可见），同时保留了 304 的省钱路径。对静态内容站，`no-store` 是过度收紧。

**练习 2**：`gzip_types` 里的 `font/woff2` 有实际收益吗？

**答案**：几乎没有。WOFF2 格式内部已经用 Brotli 压缩过字体数据，再套一层 gzip 的压缩比接近 1:1，白花 CPU。它留在列表里无害（nginx 不会把响应压得更大就使用），但从工程整洁角度可以删掉。相比之下 `font/woff`（WOFF1）压缩率有限，留在列表里是合理的。

**练习 3**：为什么这个站点可以给静态资产 `max-age=86400`，而很多现代前端项目敢用 `max-age=31536000, immutable`？

**答案**：关键差异是 URL 是否携带内容指纹。现代构建工具产出 `app.a1b2c3.js`，文件内容变则哈希变、URL 变，旧 URL 永远对应旧字节，所以可以「缓存一年且永不验证」。mdBook 的资产是固定文件名（`book.js`、`highlight.css`），rebuild 后 URL 不变、字节已变，长缓存加 immutable 会让长期不重启的浏览器一直用旧脚本。1 天是「缓存收益」与「陈旧窗口」之间的折中——nginx.conf L24-L27 的注释记录了这层推理。

### 4.4 非特权容器运行：uid 101、8080 与 no-new-privileges

#### 4.4.1 概念说明

前三个模块都在加固「内容如何被安全地服务」，这个模块加固「容器本身」。三条防线：

1. **非 root 运行**。官方 `nginx` 镜像为了绑定 80 端口以 root 启动 master 进程；`nginxinc/nginx-unprivileged` 是同一镜像的重配置版：直接以 uid 101（nginx 用户）运行、监听 8080。这样容器内没有 root 进程，即使被攻破，攻击者拿到的也只是一个无特权用户。
2. **非特权端口**。8080 > 1024，绑定不需要 `CAP_NET_BIND_SERVICE` 能力，容器可以做到「零附加能力」运行。宿主机想用 80/3000 暴露服务，交给端口映射即可——映射是 Docker 守护进程（root）做的，容器内进程无需任何特权。
3. **`no-new-privileges`**。这是 Linux 内核的 NO_NEW_PRIVS 标志（经 prctl 设置）：进程及其后代通过 `exec` 启动新程序时**永远无法获得比现在更多的权限**——setuid 位的二进制不再生效、sudo 不再可能。它堵的是「容器内提权」这条路：假设攻击者已经在容器内执行了任意代码，也无法借镜像里某个 setuid 工具变成 root。

配合 u4-l2 已建立的事实——最终镜像里没有 Rust 工具链、没有 mdbook、没有书源，只有静态 HTML 和 nginx——攻击面被压到极小。

#### 4.4.2 核心流程

从镜像到运行的安全链路：

```text
构建期:
  FROM nginxinc/nginx-unprivileged   # 基镜像自带 uid 101 + 8080 约定
  COPY --chown=nginx:nginx site → /usr/share/nginx/html   # 文件属主对齐运行用户
  EXPOSE 8080 / HEALTHCHECK(wget)

运行期 (docker compose up):
  容器以 uid 101 启动 nginx          # 无 root、无附加 capability
  security_opt: no-new-privileges    # 内核禁止后续任何提权
  宿主 ${PORT:-3000} → 容器 8080     # 特权端口需求由 Docker 守护进程代劳
  healthcheck 周期性 wget 自检       # restart: unless-stopped 兜底拉起
```

#### 4.4.3 源码精读

Dockerfile runtime 阶段的基镜像选择与理由：

[docker/Dockerfile:L83-L85](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L83-L85) —— 注释说明：nginx-unprivileged 是官方镜像重配置为 uid 101 运行、监听 8080 的版本，因此容器不需要 root，也不需要 NET_BIND_SERVICE 能力；`FROM` 行锁 `1.27` 大版本的 alpine 变体（版本钉住策略与 u4-l2 的工具链钉住一致）。

[docker/Dockerfile:L87-L88](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L87-L88) —— 两个 COPY 完成装配：本讲的 nginx.conf 覆盖镜像默认的 server 配置；`--chown=nginx:nginx` 把 site/ 产物的属主从 root 改成运行用户，与 uid 101 对齐（镜像内没有 root 属主的残留内容）。

[docker/Dockerfile:L90-L93](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/Dockerfile#L90-L93) —— `EXPOSE 8080` 是文档性声明；HEALTHCHECK 用镜像内自带的 busybox `wget` 探测 `127.0.0.1:8080/`，30 秒一次、3 次失败判 unhealthy。

compose 侧的运行时约束：

[docker/compose.yaml:L13-L16](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/compose.yaml#L13-L16) —— 注释点明设计：宿主端口可配置（`${PORT:-3000}` 缺省 3000，与本地开发端口一致），容器内**恒为** 8080——因为 nginx-unprivileged 的非 root 进程只能绑非特权端口，宿主侧想换端口动映射即可，镜像不用动。

[docker/compose.yaml:L18](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/compose.yaml#L18) —— `restart: unless-stopped`：进程崩溃自动拉起，除非运维显式 `docker compose stop`。

[docker/compose.yaml:L20-L21](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/compose.yaml#L20-L21) —— `security_opt: no-new-privileges:true`，即 4.4.1 的第三条防线：容器内进程从此无法通过 exec 获得任何额外权限。

[docker/compose.yaml:L23-L28](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/compose.yaml#L23-L28) —— compose 层的 healthcheck 与 Dockerfile 内的 HEALTHCHECK 内容相同（双保险：两种启动路径都带自检），`start_period: 5s` 给容器启动宽限期，探测期间的失败不计入重试。

最后是文档里「有意未启用」的加固项——工程判断的好样本：

[docker/README.md:L62-L66](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/README.md#L62-L66) —— Notes 承认还可以给服务加 `read_only: true`（整个根文件系统只读），但那需要为 nginx 的 cache 与 pid 路径挂 tmpfs；作者选择「不发布没测试过的配置」而留空。这与 u2-l2 见过的「宁缺毋滥」工程风格一脉相承。

#### 4.4.4 代码实践

> 本实践需要本地 Docker 环境，命令与现象**待本地验证**。

1. **实践目标**：用 `docker inspect` 与 `docker exec` 验证容器确实以非 root 运行、确实带 NO_NEW_PRIVS 标志。

2. **操作步骤**：

   ```bash
   docker compose -f docker/compose.yaml up --build -d

   # 1) 运行用户是谁？
   docker exec rust-training-books id
   # 预期: uid=101(nginx) gid=101(nginx) ...

   # 2) compose 注入的 security_opt 是否生效？
   docker inspect rust-training-books --format '{{.HostConfig.SecurityOpt}}'
   # 预期: [no-new-privileges:true]

   # 3) 进程视角验证（NO_NEW_PRIVS 会出现在 /proc 自状态里，可借 ps 看用户列）：
   docker exec rust-training-books ps -o user,pid,comm
   # 预期: 所有 nginx 进程的 user 列都是 nginx，没有 root

   # 4) 端口映射两端：
   docker port rust-training-books
   ```

3. **需要观察的现象**：`id` 输出 uid 101；SecurityOpt 含 `no-new-privileges:true`；进程列表无 root；端口映射形如 `3000->8080/tcp`。

4. **预期结果**：与 4.4.2 的链路逐项吻合。若跳过 compose 直接 `docker run`（不带 `--security-opt no-new-privileges`），第 2 步将为空——这说明该防线来自 compose 文件而非镜像本身，是部署方必须保留的配置。

5. 无 Docker 环境时的替代实践：阅读 [docker/README.md:L62-L66](https://github.com/microsoft/RustTraining/blob/9d19c482d66ef3995dca794bda74c7852134e0b7/docker/README.md#L62-L66)，写 3-5 句话分析「read_only + tmpfs」为什么能进一步加固、以及作者不启用它的理由是否成立。

#### 4.4.5 小练习与答案

**练习 1**：为什么容器内监听 8080 而不是 80？宿主机想用 80 端口访问怎么办？

**答案**：Linux 绑定 <1024 的端口需要 `CAP_NET_BIND_SERVICE`，默认只有 root 拥有；非 root 的 uid 101 只能绑 8080 这类非特权端口。监听 80 意味着要么以 root 跑 nginx（放弃第一道防线），要么给容器加能力（扩大攻击面）。宿主机想用 80，在端口映射层解决：`ports: ["80:8080"]`——映射由 root 身份的 Docker 守护进程完成，容器内进程依旧无特权。

**练习 2**：`no-new-privileges:true` 具体防的是什么攻击？它和「以非 root 用户运行」防的是同一件事吗？

**答案**：不是同一件事，二者是纵深防御的两层。「非 root 运行」防的是**当下**——进程现在没有特权，攻破它拿到的权限有限；`no-new-privileges` 防的是**将来**——内核保证该进程及其后代无论 exec 什么程序都无法获得比当前更多的权限，即使镜像里潜伏着带 setuid 位的二进制、或攻击者想借 sudo/su 提权也会失败。前者约束起点，后者封死上升通道。

**练习 3**：Dockerfile 里已有 HEALTHCHECK，compose.yaml 为什么还要写一遍 healthcheck？

**答案**：两处生效路径不同。Dockerfile 的 HEALTHCHECK 烧进镜像，任何 `docker run`（如 docker/README.md 的无 Compose 用法）都带自检；compose 的 healthcheck 是编排层覆盖，供 `docker compose ps`/依赖条件等场景使用。两边写同样的 wget 探测是刻意的双保险——两条部署路径都不依赖对方记得配置，代价只是几行重复。

## 5. 综合实践

把本讲四个模块串起来的任务：**给站点加上 Strict-Transport-Security（HSTS）响应头，走完「修改配置 → 重建镜像 → 验证 → 复盘继承规则」的完整闭环**。

> 本实践需要本地 Docker 环境。所有命令与预期**待本地验证**。

**背景**：HSTS 头（`Strict-Transport-Security: max-age=31536000`）告诉浏览器「本站只准用 HTTPS 访问」，是 HTTPS 部署的标配。本仓库的自托管场景假设部署在内部网络的 HTTPS 反代之后，容器本身只出 HTTP——所以这个头由本容器发只是练习（严格来说应由 HTTPS 终结层添加），但作为「全站新增一个安全头」的操练对象非常合适。

**步骤**：

1. 修改 `docker/nginx.conf`，在**两个** location 各加一行（这是本实践的核心考察点）：

   ```nginx
   # location / 内，与现有四行 add_header 并列:
   add_header Strict-Transport-Security "max-age=31536000" always;
   # 正则 location 内，与现有三行并列，同样加这一行
   ```

   只加一个 location 是**错误答案**——由于 4.2 的继承规则，加在 server 级两个 location 都不生效，加在一个 location 则另一半响应缺头。

2. 重建并启动：

   ```bash
   docker build -f docker/Dockerfile -t rust-training:local .
   docker run --rm -d --name rt-nginx -p 3000:8080 rust-training:local
   ```

3. 验证两类响应、两种状态码都带上了新头：

   ```bash
   curl -sI http://localhost:3000/            | grep -i strict-transport   # HTML, 200
   curl -sI http://localhost:3000/no-such     | grep -i strict-transport   # 404，验证 always
   # 再从页面里取一个真实 .css 路径验证静态资产 location
   ```

4. **复盘写作**（本实践的交付物）：写一段 150 字左右的说明，回答——为什么 nginx.conf L12-L14 的注释作者选择「每个 location 重复声明」而不是「server 级声明一次」？你的 HSTS 实验中哪一步如果偷懒会导致什么不一致？参考要点：当前层级出现任一 `add_header` 即切断继承；两个 location 都各自持有 add_header（`Cache-Control` / `expires`），server 级声明必然被双双顶掉；重复声明的代价是「新增头要改两处、漏改即不一致」，这个代价被注释明文记录，换来的是配置行为的局部可读性——读单个 location 即可知它发哪些头。

5. 还原现场：`git checkout -- docker/nginx.conf`，`docker rm -f rt-nginx`。

## 6. 本讲小结

- `try_files $uri $uri/ $uri.html =404` 用三级候选（字面文件 → 目录+index → 补 `.html`）重建了 GitHub Pages 的无扩展名链接宽容；`docker.yml` 的冒烟断言直接守护这一行为，而 xtask 开发服务器刻意不做这层兼容。
- nginx 的 `add_header` 继承规则是「当前层有声明则父层全部作废」——两个 location 各有自己的头，迫使安全头在每个 location 显式重复；`always` 参数保证 404 等错误响应也带头。
- 缓存按资产分治：HTML 用 `no-cache`（每次验证、可 304）保证 rebuild 立即可见；静态资产用 `expires 1d` 折中——mdBook 资产 URL 无内容哈希、rebuild 后同 URL 新字节，因此不能用 `immutable`。gzip 以 1KB 门槛与显式类型列表控制压缩收益。
- 运行时三防线：nginx-unprivileged 以 uid 101 + 8080 免 root 免特权能力，compose 的 `no-new-privileges:true` 在内核层封死提权通道，healthcheck 双保险守护可用性；`read_only` 是文档明示「有意未测试、故不启用」的下一层加固。
- 日志走 stdout/stderr 的容器惯例把「无状态容器」的理想又推进一步，也为未来的只读根文件系统铺路。

## 7. 下一步学习建议

本讲之后，部署篇只剩两讲，建议按依赖顺序推进：

1. **u4-l4 综合实战：向仓库添加一本新书**——把本讲的 nginx/Docker 链路与 u2 的构建链路打通：新书注册进 BOOKS 后，落地页、批量构建、Pages、Docker（包括本讲的 nginx 与 CI 冒烟）全链路自动收录，无需改任何一行基础设施代码。届时可以回头验证：新书的无扩展名链接同样被 try_files 兜住。
2. **u4-l5 质量守护与贡献流程**——从「维护者」视角收尾：CI 防线的盲区（如 u4-l1 提到的「单本书构建失败仍退出 0」）、CLA 与双许可证的复用边界。
3. 想继续深挖本讲主题的读者，可以对照阅读：nginx 官方文档中 `add_header`、`try_files`、`gzip` 三节（确认本讲陈述的继承规则与默认状态码列表）；以及 mdBook 文档里关于输出结构（`index.html`、`print.html`、资产布局）的部分，思考 `print.html` 这类特殊页面在本配置下如何被 try_files 解析。
