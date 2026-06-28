# MuPDF 是什么：项目定位与支持格式

## 1. 本讲目标

本讲是整本《MuPDF 源码学习手册》的第一篇。读完本讲后，你应该能够：

- 用一句话说清 MuPDF 是什么、它解决什么问题；
- 列举 MuPDF 支持读写的主要文档格式，并能说出每种格式对应哪个源码模块；
- 理解 MuPDF 的「双层架构」（通用层 fitz + 格式专用层），为后续阅读源码建立宏观认知；
- 区分 GNU AGPL 开源许可证与商业授权的差别，知道在什么场景下该选哪一个。

本讲不涉及任何编译和运行，只要求你**读懂项目说明和目录结构**。这是后续所有讲义的地基。

---

## 2. 前置知识

在开始前，建议你大致了解以下概念（不知道也没关系，本讲会顺带解释）：

- **文档格式**：PDF、XPS、EPUB、图片等都是把「文字 + 图片 + 排版信息」编码成文件的不同规范。MuPDF 的核心工作就是把这些规范「翻译」成屏幕上的像素或别的格式。
- **C 语言与库（library）**：MuPDF 用可移植的 C 语言写成，编译后产出一个 `.so`/`.a`/`.dll` 库，其他程序可以链接它来获得处理文档的能力。
- **开源许可证（License）**：规定了别人能怎样使用、修改、分发这份代码的法律条款。MuPDF 用的是 AGPL，这是一种「强 copyleft」许可证，后面会详细讲。
- **框架（framework）**：相对于「一个只能看 PDF 的程序」，MuPDF 自我定位为一个「框架」——它既提供现成的查看器和命令行工具，也提供可被二次开发的库。

如果你完全没接触过文档处理，可以把 MuPDF 类比成「文档界的瑞士军刀」：一把工具，既能读、能转、能看，又能被拆开拿来造新工具。

---

## 3. 本讲源码地图

本讲主要阅读「项目级文档」而非实现代码。涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `README` | 项目根目录的简短说明：定位、许可证、如何报 bug。注意它没有扩展名。 |
| `docs/guide/what-is-mupdf.md` | 官方对 MuPDF 的完整介绍：支持格式、查看器、命令行工具、各语言绑定。 |
| `docs/license.md` | 许可证说明：AGPL 开源条款与商业授权两条路。 |
| `COPYING` | 完整的 GNU AGPL v3 许可证正文。 |
| `source/fitz/document-all.c` | **关键**：把所有「格式处理器」注册进框架的入口文件，是「支持格式清单」的权威来源。 |
| `include/mupdf/fitz/config.h` | 用 `FZ_ENABLE_*` 宏控制每种格式是否参与编译，体现「可裁剪」设计。 |

> 提示：本讲引用的永久链接基于当前 HEAD `39101dd8179599d5b9653e7a33f157c08e5614eb`。如果后续仓库更新，行号可能变化，届时请以实际代码为准。

---

## 4. 核心概念与源码讲解

### 4.1 项目简介与定位

#### 4.1.1 概念说明

MuPDF 是 **Artifex Software** 公司开发的一个**轻量级开源文档处理框架**。它的名字里虽然带「PDF」，但它远远不止能处理 PDF。从官方说明可以看出，它的定位是「查看、转换、操作 PDF、XPS 和电子书文档」的通用框架。

所谓「框架」，意味着它对外提供**三种形态**的产品：

1. **查看器（viewers）**：直接给最终用户用的图形界面程序，覆盖 Linux/Windows/Android 等平台；
2. **命令行工具（command line tools）**：以 `mutool` 为统一入口的命令行瑞士军刀；
3. **软件库（library）**：用可移植 C 写成的核心库，供开发者二次开发，并有 JavaScript/Java/Python 等语言绑定。

这三者共享同一个底层 C 库，因此「在命令行里能做的事，调用库也能做」，这是理解 MuPDF 价值的关键。

#### 4.1.2 核心流程：双层架构

MuPDF 的代码在结构上分成两层，理解这一点能帮你迅速看懂后续所有源码：

```
┌──────────────────────────────────────────────┐
│  应用层：mutool / 查看器 / 你自己的程序          │
├──────────────────────────────────────────────┤
│  fitz 通用层 (source/fitz, include/mupdf/fitz) │
│  context / document / page / device /          │
│  pixmap / stream / store ... 统一抽象           │
├──────────────────────────────────────────────┤
│  格式专用层：pdf / xps / html / cbz / svg       │
│  (source/pdf, source/xps, source/html ...)     │
└──────────────────────────────────────────────┘
```

- **fitz 通用层**：定义了一套与具体格式无关的抽象接口（怎么打开文档、怎么数页、怎么渲染一页）。无论底层是 PDF 还是 EPUB，上层 API 都长得一样。
- **格式专用层**：每种格式自己实现「如何把本格式翻译成 fitz 抽象」。PDF 的实现在 `source/pdf`，XPS 在 `source/xps`，电子书类（EPUB/MOBI/HTML…）大多在 `source/html`。

这样的设计带来的最大好处是：**写一份应用代码，就能同时处理十几种格式**。这也是为什么后续讲义会花大量篇幅在 fitz 通用层上。

#### 4.1.3 源码精读

**① 根目录 `README` 的定位描述**——一句话给出 MuPDF 的核心定位：

[README:L1-L4](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/README#L1-L4) — 这段明确写到「MuPDF is a lightweight open source software framework for viewing and converting PDF, XPS, and E-book documents」，即「一个用于查看和转换 PDF、XPS 与电子书的轻量级开源软件框架」。注意三个关键词：lightweight（轻量）、framework（框架）、viewing and converting（查看与转换）。

**② 官方文档的完整定位**——把「框架三形态」说得更清楚：

[docs/guide/what-is-mupdf.md:L1-L6](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/guide/what-is-mupdf.md#L1-L6) — 这里指出 MuPDF 提供了「多种平台的查看器、若干命令行工具，以及一个用于构建工具和应用的软件库」。这正是上一节「三形态」说法的出处。

#### 4.1.4 代码实践

**实践目标**：用自己的话复述 MuPDF 的定位，确认你真的读懂了，而不是把英文原句抄一遍。

**操作步骤**：

1. 用编辑器打开项目根目录的 `README` 文件，读「ABOUT」和「LICENSE」两段。
2. 打开 `docs/guide/what-is-mupdf.md`，读开头到「## Formats」之前的部分。
3. 关掉文件，**凭记忆**写一段不超过 200 字的中文项目简介，要求包含：它是什么、能处理哪几大类格式、它提供哪三种产品形态。

**需要观察的现象 / 预期结果**：

- 你应该能不依赖原文写出三个关键信息点：①轻量级开源框架；②支持 PDF/XPS/电子书/图片等；③提供查看器、命令行工具、库三种形态。
- 如果写不出「三种形态」或漏掉「框架（而不只是一个 PDF 阅读器）」这个定位，说明还没抓住重点，回去重读 what-is-mupdf.md 第 3–6 行。

#### 4.1.5 小练习与答案

**练习 1**：MuPDF 是「一个 PDF 阅读器」还是「一个文档处理框架」？说出依据。

> **参考答案**：是一个文档处理框架。依据是 README 第 3–4 行明确用了「software framework」，且 what-is-mupdf.md 第 3–6 行说明它同时提供查看器、命令行工具和库。「PDF 阅读器」只是它的应用之一，不能概括它的全部能力。

**练习 2**：MuPDF 代码为什么分成 fitz 通用层和格式专用层？这样设计对开发者有什么好处？

> **参考答案**：为了让上层应用用「一套 API」处理多种格式。通用层定义与格式无关的抽象（document/page/device 等），格式层各自实现翻译逻辑。好处是开发者写一次渲染代码，就能同时支持 PDF、XPS、EPUB 等十几种格式，新增格式时上层代码无需改动。

---

### 4.2 支持格式清单

#### 4.2.1 概念说明

MuPDF 最显著的特点是「一个框架，多种格式」。要理解它是如何做到的，需要先认识一个核心机制：**文档处理器（document handler）**。

每种可被 MuPDF 打开的格式，都对应一个 `fz_document_handler` 结构体，里面装着一组函数指针（例如「如何识别这种格式」「如何打开」「是否需要密码」等）。MuPDF 启动时，通过一个注册函数把这些 handler 登记进一张表；当用户要打开某个文件时，框架就遍历这张表，找到能处理该文件的 handler，再委托它去解析。

因此，**「MuPDF 支持哪些格式」这个问题，最权威的答案不在文字说明里，而在「handler 注册表」这份源码里**。

#### 4.2.2 核心流程：从格式到源码模块

一个格式从「被支持」到「被打开」，大致经过：

1. **声明 handler**：在某个格式模块的 `.c` 文件里定义一个全局的 `fz_document_handler xxx_document_handler`。
2. **注册 handler**：在 `document-all.c` 的 `fz_register_document_handlers()` 里，用 `fz_register_document_handler(ctx, &xxx_document_handler)` 把它登记进表。每条注册都被一个 `FZ_ENABLE_XXX` 宏包裹，可在编译时按需裁剪。
3. **识别与打开**：用户调用 `fz_open_document(ctx, path)`，框架按扩展名（magic）或文件内容匹配到对应 handler，调用它的打开回调，返回一个统一的 `fz_document` 对象。

要点：**handler 定义在哪个 `.c` 文件，就说明这种格式的解析逻辑在那个源码模块里**。这正是「格式 → reader 模块」对照表的依据。

#### 4.2.3 源码精读

**① 官方对外公布的格式清单**：

[docs/guide/what-is-mupdf.md:L8-L19](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/guide/what-is-mupdf.md#L8-L19) — 这里列出了面向用户的格式：PDF、XPS/OpenXPS、EPUB（无 DRM 的 2.0，对 3.0 有限支持）、Mobipocket(MOBI)、FictionBook 2(FB2)、ComicBook(CBZ/CBT)、图片（TIFF/JPEG/PNG 等）、SVG（仅有限子集）。

**② handler 注册表（权威清单）**——所有格式处理器的外部声明：

[source/fitz/document-all.c:L25-L38](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L25-L38) — 这里用 `extern` 声明了 14 个 handler 变量。每个变量名（如 `pdf_document_handler`、`epub_document_handler`）就是一种被支持的格式。

**③ 实际注册逻辑**——逐个登记，并用宏裁剪：

[source/fitz/document-all.c:L40-L80](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L40-L80) — `fz_register_document_handlers()` 函数体。可以看到每行注册都被 `#if FZ_ENABLE_XXX` 包裹；唯独最后的 `gz_document_handler`（处理 gzip 压缩包）没有宏保护，说明它无条件注册——因为任意格式都可能被 gzip 压缩过。

**④ 编译期裁剪开关**：

[include/mupdf/fitz/config.h:L196-L242](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L196-L242) — 这里把每个 `FZ_ENABLE_XXX` 默认设为 `1`（启用）。若你想做一个只支持 PDF 的精简版 MuPDF，可以在编译前把这些宏里无关的设为 `0`，从而减小二进制体积。

**⑤ 「格式 → 源码模块」对照表**（基于 handler 的定义位置整理）：

| 格式 | handler 变量 | 定义所在文件（解析逻辑模块） | 裁剪宏 |
|------|-------------|------------------------------|--------|
| PDF | `pdf_document_handler` | `source/pdf/pdf-xref.c` | `FZ_ENABLE_PDF` |
| XPS / OpenXPS | `xps_document_handler` | `source/xps/xps-doc.c` | `FZ_ENABLE_XPS` |
| SVG（子集） | `svg_document_handler` | `source/svg/svg-doc.c` | `FZ_ENABLE_SVG` |
| ComicBook（CBZ/CBT） | `cbz_document_handler` | `source/cbz/mucbz.c` | `FZ_ENABLE_CBZ` |
| 图片（TIFF/JPEG/PNG…） | `img_document_handler` | `source/cbz/muimg.c` | `FZ_ENABLE_IMG` |
| HTML | `html_document_handler` | `source/html/html-doc.c` | `FZ_ENABLE_HTML` |
| XHTML | `xhtml_document_handler` | `source/html/html-doc.c` | `FZ_ENABLE_HTML` |
| EPUB | `epub_document_handler` | `source/html/epub-doc.c` | `FZ_ENABLE_EPUB` |
| Mobipocket（MOBI） | `mobi_document_handler` | `source/html/html-doc.c` | `FZ_ENABLE_MOBI` |
| FictionBook 2（FB2） | `fb2_document_handler` | `source/html/html-doc.c` | `FZ_ENABLE_FB2` |
| 纯文本 TXT | `txt_document_handler` | `source/html/txt.c` | `FZ_ENABLE_TXT` |
| Office（doc/ppt/xls 等） | `office_document_handler` | `source/html/office.c` | `FZ_ENABLE_OFFICE` |
| Markdown | `md_document_handler` | `source/html/md.c` | `FZ_ENABLE_MD` |
| GZip 压缩包 | `gz_document_handler` | `source/fitz/gz-doc.c` | （始终注册） |

> **关键洞察**：表里右侧大量格式都落在 `source/html/` 目录下。这是因为 EPUB、MOBI、FB2、Office、Markdown 等电子书/富文本格式**共用同一套 HTML 排版引擎**（由 `FZ_ENABLE_HTML_ENGINE` 控制），所以它们的 handler 都聚集在 HTML 模块附近。图片的 handler 则复用了 `source/cbz/` 的代码（因为「一页一张图」的 ComicBook 和「单张图片」在渲染逻辑上高度相似）。这些复用关系是后续进阶讲义会反复涉及的内容。

#### 4.2.4 代码实践

**实践目标**：自己动手从源码中「挖」出格式清单，而不是死记本讲给出的表格。这样你以后面对任何新版本 MuPDF，都能独立确认它支持什么。

**操作步骤**：

1. 在项目根目录运行下面这条搜索命令，列出所有定义了 handler 的 `.c` 文件：
   ```bash
   grep -rn "fz_document_handler .*_document_handler =" source/
   ```
2. 把输出整理成一张「handler 变量 → 所在文件」的对照表（就像本讲 4.2.3 的表格那样）。
3. 再运行下面这条命令，确认每个 handler 是否真的被注册：
   ```bash
   grep -n "fz_register_document_handler(ctx," source/fitz/document-all.c
   ```
4. 挑选一个你没听过的格式（例如 FB2 或 Office），打开它对应的源码文件（如 `source/html/office.c`）顶部，读一下注释，了解它的支持程度。

**需要观察的现象 / 预期结果**：

- 第 1 步应输出约 14 行，每行对应一种格式 handler 的定义点。
- 第 3 步应输出约 14 行注册调用，且大部分被 `#if FZ_ENABLE_XXX` 包裹。
- 你会发现「官方文档列出的格式」是用户友好的子集（如 what-is-mupdf.md 没专门提 TXT/Office/Markdown/GZip），而源码注册表才是**完整且权威**的清单。

> 说明：以上命令是只读搜索，不会修改任何源码，可以放心执行。若运行环境没有 `grep`，可改用代码编辑器的全局搜索功能，搜索模式 `fz_document_handler .*_document_handler =`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `gz_document_handler` 在注册时没有 `FZ_ENABLE_XXX` 宏保护？

> **参考答案**：因为 gzip 只是一种「外层压缩容器」，任何格式的文件都可能先被 gzip 压缩（例如 `file.pdf.gz`）。MuPDF 需要无条件地能识别并解掉这层压缩，再交给真正的格式 handler，所以它必须始终注册，不能被裁剪掉。

**练习 2**：EPUB 的解析代码在 `source/html/` 而不是 `source/epub/`，这说明什么？

> **参考答案**：说明 EPUB 复用了 HTML 排版引擎（EPUB 本质上就是一堆 XHTML + 图片打包成的 zip）。MuPDF 把多种「基于 HTML 的电子书格式」统一归到 `source/html/` 模块下，共享同一套排版代码，避免重复实现。

**练习 3**：如果你只想编译一个「只支持 PDF」的精简版 MuPDF，应该改哪里？

> **参考答案**：在编译前，把 `include/mupdf/fitz/config.h`（或通过编译参数 `-DFZ_ENABLE_XXX=0`）里除 PDF 外的 `FZ_ENABLE_XXX` 宏设为 `0`。这样 `fz_register_document_handlers` 里对应分支会被预处理器剔除，最终二进制只包含 PDF 相关代码。注意 HTML/EPUB/MOBI 等依赖 `FZ_ENABLE_HTML_ENGINE`，关闭时要一并处理（见 config.h 第 272–293 行的相互约束）。

---

### 4.3 许可证与授权

#### 4.3.1 概念说明

在使用、分发或基于 MuPDF 做产品之前，必须先搞清楚它的许可证，否则可能踩法律红线。MuPDF 采用的是**双许可模式**：

1. **开源许可证：GNU AGPL v3**（Affero General Public License）——免费，但有严格条件；
2. **商业许可证**——付费向 Artifex 购买，免除 AGPL 的限制。

你需要根据自己产品的形态，判断能不能接受 AGPL，从而决定走哪条路。

#### 4.3.2 核心流程：AGPL 的「传染性」与商业授权的取舍

AGPL 是一种**强 copyleft（传染性）**许可证，关键规则可以用下面的决策流程理解：

```
你的项目是否使用了 MuPDF（链接/修改/分发）？
        │
        ├── 否 → 与 MuPDF 许可证无关，自由选择
        │
        └── 是 → 默认适用 AGPL v3：
                  · 你必须开源你「整个衍生作品」的源代码
                  · 且若作为网络服务对外提供，也必须开源（这是 AGPL 相对 GPL 的核心加码）
                  · 无任何担保(warranty)
                  │
                  ├── 你愿意/能够遵守上述条件？ 
                  │       ├── 是 → 免费使用 AGPL 版本即可
                  │       └── 否 → 必须购买商业许可证
```

简而言之：**AGPL 要求「用了就得把整个产品的源码也开源，包括网络服务」**。这对闭源商业产品、SaaS 服务是硬伤；对开源项目、个人学习则完全免费。商业许可证就是为「无法或不愿遵守 AGPL」的用户准备的「赎买」通道。

#### 4.3.3 源码精读

**① 根目录 README 的许可证声明**：

[README:L14-L31](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/README#L14-L31) — 这里写明：版权属于 Artifex Software（Copyright (c) 2006-2026），程序在 GNU AGPL v3 下发布（「version 3 of the License, or (at your option) any later version」），并提供商业授权联系方式 `sales@artifex.com`，还提到对独立开发者友好的「Indie Dev」选项。

**② LICENSE 正文**：

[COPYING:L1-L2](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/COPYING#L1-L2) — `COPYING` 文件就是完整的 GNU AGPL v3 正文，开头标明「Version 3, 19 November 2007」。这是法律效力的最终依据。

**③ 许可证对比说明**：

[docs/license.md:L1-L23](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/license.md#L1-L23) — 官方把两条路讲得很直白：AGPL「allows you to use MuPDF to build your own projects for free, with no warranty and no support」，但「imposes many conditions on users, including the need to release the full source code for systems built with it」；而商业许可证「completely frees users from the complexities imposed by the GNU AGPL」。

**④ 每个源码文件头部的提示**：以 document-all.c 为例：

[source/fitz/document-all.c:L1-L21](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/document-all.c#L1-L21) — 几乎每个 `.c` 文件顶部都有这段版权与许可证声明，重申 AGPL 条款，并指出「Alternative licensing terms are available from the licensor」（可向授权方获取替代的许可条款，即商业授权）。阅读源码时看到这段，就说明该文件同样受 AGPL 约束。

#### 4.3.4 代码实践

**实践目标**：把许可证条款和真实场景对应起来，培养「读代码先看 license」的习惯。

**操作步骤**：

1. 打开 `docs/license.md`，通读「Open Source / Commercial / Warranty」三节。
2. 打开 `COPYING` 的开头（前 40 行即可），感受 AGPL 正文的措辞风格。
3. 针对下面三个场景，分别判断「能否免费用 AGPL 版」「还是必须买商业授权」，并简述理由：
   - 场景 A：你写一个**开源**的命令行小工具，源码全部放在 GitHub，链接了 MuPDF。
   - 场景 B：你做一个**闭源**的商用 PDF 编辑器，打包成 App 销售。
   - 场景 C：你做一个**闭源**的网站，用户上传 PDF，服务器用 MuPDF 在线转换并返回结果（SaaS）。

**需要观察的现象 / 预期结果**：

- 场景 A：可以用 AGPL 免费版，前提是你的工具也按 AGPL 开源。
- 场景 B：闭源商用 → 不能用 AGPL 版，必须买商业授权。
- 场景 C：这是 AGPL「网络服务条款」专门针对的场景——即使不分发二进制，只要作为网络服务对外提供，也要开源整个服务。闭源 SaaS 因此也必须买商业授权。
- 完成后，把结论写成三句话存档。这就是你将来在团队里做技术选型时的判断依据。

> 说明：本实践是阅读与判断型任务，不涉及任何命令运行。许可证结论基于仓库内的 `COPYING` 与 `docs/license.md`；若用于真实商业决策，建议再咨询 Artifex 官方或法律顾问。

#### 4.3.5 小练习与答案

**练习 1**：AGPL 和普通 GPL 最大的区别是什么？为什么这个区别对 Web 服务很重要？

> **参考答案**：最大区别是 AGPL 增加了「网络使用即触发开源」的条款。普通 GPL 只在「分发」二进制时要求开源，所以把 GPL 软件跑成 Web 服务、只对外提供接口时，可以不开源；而 AGPL 专门堵住了这个漏洞——只要通过网络向用户提供服务，就必须开源整个服务。所以对 Web/SaaS 场景，AGPL 的约束比 GPL 严格得多。

**练习 2**：你在自己电脑上为了学习，编译并运行了 MuPDF，需要购买商业许可证吗？

> **参考答案**：不需要。AGPL 对「个人学习、内部使用」是免费的。商业授权只在「分发衍生作品」或「作为网络服务对外提供」且不愿开源时才必要。单纯本地学习研究完全适用 AGPL 免费条款。

**练习 3**：每个源码文件顶部的版权注释有什么实际作用？

> **参考答案**：它逐文件声明该文件受 AGPL 约束、版权归 Artifex，并提示存在商业授权替代方案。作用有二：一是法律上明确每个文件的许可状态（即使文件被单独取出也带 license 信息）；二是提醒阅读/使用者「这份代码不是公有领域代码，使用需遵守 AGPL 或购买商业授权」。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「项目速览卡片」任务。它的产出可以直接作为你给团队做 MuPDF 技术介绍的开场材料。

**任务**：制作一份一页纸的《MuPDF 项目速览》，包含以下四个板块，全部基于本仓库的真实文件：

1. **一句话定位**：用你自己的话（不超过 50 字）描述 MuPDF 是什么。依据：`README` 第 1–4 行。
2. **架构示意**：画一个 fitz 通用层 + 格式专用层的双层结构图，并标注 `source/` 下至少 4 个格式模块目录（如 `pdf`、`xps`、`html`、`cbz`）。
3. **格式对照表**：从本讲 4.2.3 的表格中挑选 6 种你最可能用到的格式，列出「格式 → handler 变量 → 源码文件 → 裁剪宏」四列。要求至少包含 PDF、EPUB、图片三种。
4. **许可证结论**：用三行写出「AGPL 适用于什么场景 / 商业授权适用于什么场景 / 我的项目该选哪个」。

**操作提示**：

- 板块 2 的目录名可以用 `ls source/` 确认。
- 板块 3 若想核对，运行 `grep -rn "fz_document_handler .*_document_handler =" source/`。
- 板块 4 的判断依据见 `docs/license.md`。

**预期结果**：得到一份结构完整、每条都能追溯到仓库具体文件/行号的项目速览。如果其中任何一条你写不出依据，说明对应模块还没读透，回到第 4 节相应小节复习。

---

## 6. 本讲小结

- MuPDF 是 Artifex Software 出品的**轻量级开源文档处理框架**，定位是查看、转换、操作 PDF/XPS/电子书/图片等多种格式。
- 它对外提供**三种形态**：多平台查看器、以 `mutool` 为入口的命令行工具、可二次开发的 C 库（并有 JS/Java/Python 绑定）。
- 代码采用**双层架构**：fitz 通用层定义与格式无关的抽象，格式专用层（`source/pdf`、`source/xps`、`source/html` 等）各自实现翻译逻辑。
- 「支持哪些格式」的**权威来源是 `source/fitz/document-all.c` 的 handler 注册表**，共 14 个 handler；其中许多电子书格式共享 `source/html/` 的 HTML 引擎。
- 每种格式都可被 `FZ_ENABLE_XXX` 宏在编译期裁剪，便于按需精简二进制。
- 许可证为**双许可**：GNU AGPL v3（免费但要求开源，含网络服务条款）与商业授权（付费免除限制）二选一。

---

## 7. 下一步学习建议

本讲只读了项目说明和目录结构，还没碰任何编译和运行。建议按以下顺序继续：

1. **下一讲 u1-l2《构建系统：如何编译 MuPDF》**：学习 Makefile / Makerules / Makelists / Makethird 的分工，亲手把库和 `mutool` 编译出来——这是后续所有「动手」讲义的前提。
2. **u1-l3《源码目录与模块布局》**：系统梳理 `include/`、`source/`、`platform/`、`thirdparty/` 的职责，把本讲的双层架构落到具体目录上。
3. **u1-l4《命令行瑞士军刀 mutool》**：解析 `source/tools/mutool.c` 的子命令分发表，理解 `mutool draw/convert/show…` 如何路由。
4. **u1-l5《第一个渲染程序：跑通 example.c》**：用官方示例串起第一条渲染链路，第一次让 MuPDF 真正「跑」起来。

> 阅读建议：在进入 u1-l2 之前，可以先在本机把仓库 clone 下来并浏览一遍 `source/` 目录，对照本讲的格式对照表找找感觉，这会让后续讲义的学习事半功倍。
