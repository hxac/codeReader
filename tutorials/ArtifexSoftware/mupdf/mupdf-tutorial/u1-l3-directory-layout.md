# 源码目录与模块布局

## 1. 本讲目标

本讲是「从零认识 MuPDF」单元的第三篇。学完之后，你应当能够：

- 看懂 MuPDF 仓库顶层每一个目录的用途，知道「想找某样东西该去哪个目录」。
- 区分 `include/mupdf/fitz`（通用层头文件）与 `include/mupdf/pdf`（PDF 专用层头文件），理解为什么 API 要这样分层。
- 说清 `source/` 下 `fitz`、`pdf`、`xps`、`html`、`cbz`、`tools` 等子目录各自负责什么。
- 认识 `platform/` 下的 `gl`、`x11`、`win32`、`wasm`、`java` 等平台 viewer 与语言绑定，以及 `thirdparty/` 里第三方依赖的组织方式。

本讲只读目录与头文件，不深入任何算法。目标是为你建立一张「项目地图」，后续每一篇讲义都会在这张地图上定位。

## 2. 前置知识

阅读本讲前，你应当已经读完 [u1-l1 MuPDF 是什么](./u1-l1-project-overview.md)。这里承接其中的两个关键认知：

1. **双层架构**：MuPDF 分为「fitz 通用层」和「格式专用层」。通用层定义了与具体格式无关的抽象（文档、页面、设备、像素图等）；每一种格式（PDF、XPS、HTML…）的专用层只负责把本格式「翻译」成通用层的抽象。本讲要做的，就是把这种架构对应到真实的目录和头文件上。
2. **构建产物**：`make` 之后会得到 `libmupdf.a`、`libmupdf-third.a` 和可执行文件 `mutool`。本讲会解释这三个产物分别来自哪些目录。

如果你还不熟悉「双层架构」这个词，把它简单理解为：**通用层是「插座」，格式层是「插头」**。所有格式插头都插进同一个插座，所以应用层只需面对插座一种接口。

下面用到一个术语需要先解释：

- **伞形头文件（umbrella header）**：一个 `.h` 文件本身几乎不写代码，只负责 `#include` 一大堆子头文件。使用者只要 `#include` 这一个伞形头，就等于把一整套 API 都引入了。MuPDF 的 `fitz.h` 和 `pdf.h` 就是这种伞形头。

## 3. 本讲源码地图

本讲主要「读目录」，但也会落点到下面几个真实文件上：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README` | 项目一句话简介，是顶层目录的「门牌」。 |
| `include/mupdf/fitz.h` | 通用层伞形头，把约 50 个 fitz 子头文件按 I/O、资源、文档、输出等分组引入。 |
| `include/mupdf/pdf.h` | PDF 专用层伞形头，**先引入 fitz.h，再叠加** PDF 专有头文件，体现「专用层建立在通用层之上」。 |
| `include/mupdf/html.h` | HTML 类格式的额外公共头（因为 HTML 引擎可被编译裁剪，所以单独成头）。 |
| `docs/guide/what-is-mupdf.md` | 官方对支持格式、viewer、命令行的总览说明，是核对目录用途的权威参考。 |
| `.gitmodules` | 列出 `thirdparty/` 下 18 个第三方依赖子模块的清单。 |
| `platform/` | 各平台 viewer（`gl`/`x11`/`win32`）与语言绑定（`wasm`/`java`）的源码。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**顶层目录概览**、**fitz 与 pdf 头文件分层**、**平台与第三方目录**。

### 4.1 顶层目录概览

#### 4.1.1 概念说明

像 MuPDF 这样的大型 C 项目，往往把「不同关注点」放进不同的顶层目录。这样无论是构建系统、使用者还是二次开发者，都能凭目录名快速定位。MuPDF 仓库根目录下有这几个关键目录：

| 目录 | 一句话用途 |
| --- | --- |
| `include/` | 对外公开的 C 头文件，是「API 的契约」。使用者只该 `#include` 这里面的文件。 |
| `source/` | 库与工具的实现代码（`.c`），即「API 背后真正干活的代码」。 |
| `platform/` | 各平台的图形 viewer 与跨语言绑定（OpenGL/X11/Win32/WASM/Java）。 |
| `thirdparty/` | 以 git 子模块形式内置的第三方依赖（freetype、zlib、openjpeg 等）。 |
| `docs/` | 文档源码（基于 Sphinx，发布到 readthedocs）。 |
| `resources/` | 内置资源（如基础 14 字体、CMap、图标），运行时会被打包进库或 viewer。 |
| `scripts/` | 构建期/开发期脚本（字符编码表、打包脚本等）。 |
| `generated/` | 构建过程中生成的产物目录（如生成的资源头）。 |

此外根目录还有几个非目录的关键文件：`Makefile`、`Makerules`、`Makelists`、`Makethird` 四件套负责构建（详见 [u1-l2 构建系统](./u1-l2-build-system.md)）；`COPYING` 是 AGPL 许可证全文；`CHANGES` 是版本变更记录；`README` 是项目门牌。

> 一个有用的直觉判断：**「想知道有什么能力 → 看 `include/`；想知道能力怎么实现 → 看 `source/`；想知道能不能在我的平台跑起来 → 看 `platform/`；想知道依赖了哪些库 → 看 `thirdparty/`」**。

#### 4.1.2 核心流程

当你拿到 MuPDF 源码，定位信息的流程通常是：

```text
1. 读 README → 知道项目是什么、怎么编译。
2. 看 include/mupdf/ → 知道库提供哪些 API（fitz.h / pdf.h 是入口）。
3. 看 source/<同名子目录>/ → 找到对应 API 的实现。
4. 看 platform/<平台>/ → 找到该平台的 viewer 或绑定。
5. 看 thirdparty/<库名>/ → 找到某个能力的底层第三方实现。
6. 看 docs/ → 找到官方说明、示例与手册。
```

这条「按目录名顺藤摸瓜」的路径，是后续阅读任何模块的基础。

`README` 本身非常简短，开门见山给出了项目定位与许可证：[README](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/README) ——它把 MuPDF 描述为 "a lightweight open source software framework for viewing and converting PDF, XPS, and E-book documents"，并指向了文档站点。

#### 4.1.3 源码精读

我们用一张精简的目录树来固化「顶层目录概览」（这里只列到关键二级目录）：

```text
mupdf/
├── README              # 项目门牌：定位 + 许可证
├── Makefile            # 构建（详见 u1-l2）
├── Makerules / Makelists / Makethird
├── COPYING             # AGPL 许可证全文
├── CHANGES             # 变更记录
├── include/mupdf/      # 公共 API 头文件（本讲重点）
│   ├── fitz.h          #   通用层伞形头
│   ├── fitz/           #   通用层子头（约 50 个）
│   ├── pdf.h           #   PDF 层伞形头
│   ├── pdf/            #   PDF 层子头（19 个）
│   └── html.h          #   HTML 类格式额外头
├── source/             # 实现代码
│   ├── fitz/           #   通用层实现（176 个文件，最大）
│   ├── pdf/            #   PDF 专用实现
│   ├── xps/            #   XPS 专用实现
│   ├── html/           #   HTML/EPUB/MOBI/FB2/Markdown 共用引擎
│   ├── cbz/            #   漫画书(CBZ/CBT)与图片格式
│   ├── svg/            #   SVG（受限子集）
│   ├── reflow/         #   重排文档
│   ├── tools/          #   mutool 及其子命令
│   └── helpers/        #   线程/办公库/PKCS7 辅助
├── platform/           # 平台 viewer 与绑定
│   ├── gl/  x11/  win32/  wasm/  java/
├── thirdparty/         # 18 个第三方依赖子模块
├── docs/               # Sphinx 文档源
├── resources/          # 内置字体/CMap/图标等资源
└── scripts/            # 开发与打包脚本
```

对照官方总览，可以看到目录划分与功能一一对应：`docs/guide/what-is-mupdf.md` 的 Formats 小节列出了全部支持格式——[docs/guide/what-is-mupdf.md:8-19](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/docs/guide/what-is-mupdf.md#L8-L19) 这里逐条列出了 PDF、XPS、EPUB、MOBI、FB2、ComicBook、Images、SVG。这些格式在 `source/` 下都能找到对应实现目录（`pdf/`、`xps/`、`html/`、`cbz/`、`svg/`），这正是「格式清单 → 实现目录」的直接映射。

#### 4.1.4 代码实践

**实践目标**：用仓库自带的只读信息，亲手统计每个顶层目录的文件规模，建立量化的「项目地图」。

**操作步骤**：

1. 在仓库根目录执行下面命令，统计各顶层目录的文件数量（**示例命令**，需在本地终端运行）：

   ```bash
   for d in include source platform thirdparty docs resources scripts; do
     printf "%-12s %s\n" "$d" "$(git ls-files $d | wc -l)"
   done
   ```

2. 再统计 `source/` 各子目录的文件数，体会通用层 `fitz/` 的体量：

   ```bash
   for d in source/*/; do
     printf "%-18s %s\n" "$d" "$(git ls-files $d | wc -l)"
   done
   ```

**需要观察的现象**：`source/fitz/` 的文件数应远大于任何格式专用目录；`include/` 的文件数远少于 `source/`（API 精简、实现庞大）；`thirdparty/` 如果子模块未初始化，`git ls-files` 计数可能为 0。

**预期结果**：你会得到一张「目录 → 文件数」表，能直观看到通用层是项目主体。

**待本地验证**：具体数字取决于本地是否执行过 `git submodule update --init`，所以 `thirdparty/` 的计数请在本地自行确认。

#### 4.1.5 小练习与答案

**练习 1**：你想知道 MuPDF 提供了哪些可以调用的 C 函数，应该去哪个目录找？为什么不去 `source/`？
**参考答案**：去 `include/mupdf/`。因为它是「公开 API 的契约」，声明了对外能力；`source/` 是这些声明的实现，函数签名以头文件为准。

**练习 2**：`README` 里说 MuPDF 支持 viewing 与 converting。请对应到目录，分别说出「查看器」和「转换工具」的代码大致在哪。
**参考答案**：查看器在 `platform/`（如 `platform/gl/`、`platform/x11/`）；转换工具在 `source/tools/`（`mutool` 的各子命令，如 `muconvert.c`、`mudraw.c`）。

**练习 3**：为什么 `thirdparty/` 用 git 子模块，而不是直接把第三方源码复制进主仓库？
**参考答案**：子模块让第三方代码保持独立版本管理、便于升级与对照上游，同时避免把庞大的外部源码塞进主仓库历史。克隆后需 `git submodule update --init` 才能拿到实际代码。

---

### 4.2 fitz 与 pdf 头文件分层

#### 4.2.1 概念说明

本模块是本讲的重点：把「双层架构」对应到具体的头文件组织。

MuPDF 的头文件分成两层，物理上位于 `include/mupdf/` 下的两个子目录：

- **通用层 `include/mupdf/fitz/`**：约 50 个头文件，定义与格式无关的抽象。名字 `fitz` 来自 MuPDF 早期作者的名字（"Fit of Zen"），它就是这个框架的「内核」。无论你处理 PDF 还是 XPS，最终都落到这套抽象上：`fz_context`（上下文）、`fz_document`（文档）、`fz_page`（页面）、`fz_device`（绘图设备）、`fz_pixmap`（像素图）、`fz_stream`（输入流）、`fz_store`（缓存）等。
- **PDF 专用层 `include/mupdf/pdf/`**：19 个头文件，定义 PDF 才有的概念。比如 `pdf_obj`（PDF 对象模型，PDF 的 null/int/real/string/name/array/dict 七种类型）、`xref`（交叉引用表）、`crypt`（加密）等。这些概念只对 PDF 有意义。

两层之间是**单向依赖**：PDF 层依赖通用层，反之不成立。这一点在伞形头里看得最清楚。

> 为什么 HTML 没有一个像 `pdf.h` 那样被默认引入的伞形头？因为 HTML 排版引擎是**可选**的（可用 `FZ_ENABLE_HTML=0` 在编译期裁剪），所以它有独立的 [include/mupdf/html.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/html.h)，不会被 `fitz.h` 强制引入——只有真正需要 HTML 类格式的代码才会去 include 它。

#### 4.2.2 核心流程

两层头文件的「加载流程」可以这样描述：

```text
应用代码
  │
  │  #include "mupdf/fitz.h"        ← 只用通用层抽象（能处理所有格式）
  ▼
fitz.h（通用层伞形头）
  │  按 I/O → 资源 → 设备 → 文档 → 输出 分组，引入约 50 个子头
  ▼
通用层能力就绪：context / document / page / device / pixmap / stream ...

如果还要直接操作 PDF 内部对象：
  │
  │  #include "mupdf/pdf.h"
  ▼
pdf.h（PDF 层伞形头）
  │  第一步：#include "mupdf/fitz.h"   ← 先拿到通用层
  │  第二步：叠加 pdf/object.h、xref.h、crypt.h、page.h ...   ← 再加 PDF 专有
  ▼
PDF 专有能力就绪：pdf_obj、xref、加密、表单、标注 ...
```

关键点是 **`pdf.h` 必须先 include `fitz.h`**，这就在头文件层面固化了「PDF 层建立在通用层之上」的依赖方向。

#### 4.2.3 源码精读

先看通用层伞形头 `fitz.h`。它用注释把子头文件分成若干组，下面是几组关键 include：

- **核心基础**（无分组注释，位于顶部）：[include/mupdf/fitz.h:30-36](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz.h#L30-L36) 引入 `version/config/system/context/output/log/options`，其中 `context.h`（`fz_context`）是几乎所有调用的第一个参数，`config.h` 承载 `FZ_ENABLE_*` 编译裁剪开关。
- **I/O 组**：[include/mupdf/fitz.h:50-57](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz.h#L50-L57) 以 `/* I/O */` 注释开头，引入 `buffer/stream/compress/compressed-buffer/filter/archive/heap`，这是所有「读字节、解压、解码」能力的基础。
- **资源组**：[include/mupdf/fitz.h:60-71](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz.h#L60-L71) 以 `/* Resources */` 开头，引入 `store/color/pixmap/image/bitmap/shade/font/path/text/separation/glyph`，定义文档里会用到的「资源对象」。
- **设备组**：[include/mupdf/fitz.h:73-75](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz.h#L73-L75) 引入 `device/display-list/structured-text`，定义「如何把页面内容翻译到不同后端」。
- **文档组**：[include/mupdf/fitz.h:80-85](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz.h#L80-L85) 以 `/* Document */` 开头，引入 `link/outline/document/util`，这是「打开文档、计数页数、加载页面」的统一抽象。
- **输出格式组**：[include/mupdf/fitz.h:87-97](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz.h#L87-L97) 以 `/* Output formats */` 开头，引入 `writer/band-writer/write-pixmap/output-svg` 等，对应「把文档导出成别的格式」。

> 仅凭这一个伞形头的分组注释，你就能勾勒出 MuPDF 通用层的全貌：基础 → I/O → 资源 → 设备 → 文档 → 输出。这张图在后续每一篇讲义都会反复出现。

再看 PDF 层伞形头 `pdf.h`，它清晰地演示了「先通用、后专用」的分层：

- [include/mupdf/pdf.h:26](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf.h#L26) 第一个 include 就是 `mupdf/fitz.h`，意味着 PDF 层完全建立在通用层之上。
- [include/mupdf/pdf.h:32-52](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/pdf.h#L32-L52) 随后才叠加 PDF 专有头：`object/document/parse/xref/crypt/page/resource/cmap/font/interpret/annot/form/event/javascript/clean/recolor/image-rewriter/zugferd`。注意这些概念（对象模型、xref、加密、表单、标注、JavaScript）都是 PDF 才有的，所以放在专用层。

#### 4.2.4 代码实践

**实践目标**：把通用层与 PDF 专用层的头文件分组，亲手整理出一张「头文件 → 所属层 → 类别」对照表，固化对分层的理解。

**操作步骤**：

1. 打开 `include/mupdf/fitz.h`，按 `/* ... */` 分组注释，把 50 个子头分成「核心 / I/O / 资源 / 设备 / 文档 / 输出」几类，每类列 2–3 个代表头文件。
2. 打开 `include/mupdf/pdf.h`，确认它的第一行有效 include 是 `mupdf/fitz.h`，再把其余 PDF 头文件按用途归类（对象模型 / 页面与资源 / 交互 / 写回整理）。
3. 对照 `include/mupdf/fitz/` 与 `include/mupdf/pdf/` 两个目录的实际文件数，验证你对「通用层更宽、PDF 层更窄」的印象。

**需要观察的现象**：`pdf.h` 中没有任何一个 `mupdf/pdf/*` 头会反过来引入 PDF 之外的东西；`fitz.h` 里完全不出现 `mupdf/pdf/*`，证明依赖是单向的。

**预期结果**：得到两张小表，一张描述通用层分组，一张描述 PDF 层叠加在通用层之上的结构。

**待本地验证**：分组数量与代表头文件请以你本地 `fitz.h` 的实际注释为准（不同版本可能微调）。

#### 4.2.5 小练习与答案

**练习 1**：如果一个程序只想「把任意格式文档渲染成图片」，它需要 `#include "mupdf/pdf.h"` 吗？为什么？
**参考答案**：不需要。渲染只需要通用层的 `fz_document/fz_page/fz_device/fz_pixmap`，这些都在 `fitz.h` 里。`pdf.h` 只在需要直接操作 PDF 内部对象（如 `pdf_obj`、xref）时才引入。

**练习 2**：`fz_document`（通用层）和 `pdf_document`（PDF 层）是什么关系？
**参考答案**：`pdf_document` 是 `fz_document` 的「PDF 实现」。通用层 `fz_document` 是抽象接口，PDF 层通过 handler 把 PDF 文件包装成一个 `fz_document`，让上层用同一套 API 访问。这正体现了「插座与插头」的关系。

**练习 3**：为什么 `html.h` 没有被 `fitz.h` 引入，而 `pdf.h` 里却能自由引入 `fitz.h`？
**参考答案**：HTML 引擎是可选模块（可被 `FZ_ENABLE_HTML` 裁剪），不能强制进入通用层；而 PDF 层本来就依赖通用层，且 PDF 是核心格式，所以 `pdf.h` 主动引入 `fitz.h` 没有问题。依赖方向始终是「专用层 → 通用层」。

---

### 4.3 平台与第三方目录

#### 4.3.1 概念说明

前两模块讲的是「库本身」。本模块讲两块「外围」：把库变成可视化应用的 **`platform/`**，以及库所依赖的 **`thirdparty/`**。

**`platform/`：平台 viewer 与语言绑定。** MuPDF 的核心库是平台无关的可移植 C，但用户最终要的是「能点开看的程序」和「能在我的语言里调用的绑定」。这些平台相关代码都集中在 `platform/`：

| 子目录 | 用途 |
| --- | --- |
| `platform/gl/` | 主力跨平台 viewer `mupdf-gl`，基于 OpenGL/freeglut，功能最全（目录、搜索、标注编辑、打码等）。 |
| `platform/x11/` | 传统 X11 viewer `mupdf-x11`（含 `pdfapp.c` 应用逻辑 + X11 窗口代码），兼容性好但功能少。 |
| `platform/win32/` | Windows 平台的工程文件（`.vcxproj` 等）与构建辅助。 |
| `platform/wasm/` | WebAssembly 绑定 `MuPDF.js`，供浏览器/Node/Bun 通过 JS/TS 调用。 |
| `platform/java/` | Java/JNI 绑定（含 Android），通过 JNI 桥接 C 库，也是 Android viewer 的基础。 |

**`thirdparty/`：18 个第三方依赖子模块。** MuPDF 的许多底层能力并非自己实现，而是复用成熟开源库。它们以 git 子模块形式内置，编译时可选「系统库」或「内置源码」二选一（见 [u1-l2 构建系统](./u1-l2-build-system.md)）。

#### 4.3.2 核心流程

`platform/` 与 `thirdparty/` 各自的协作流程：

```text
【平台层 platform/】
  核心库 (libmupdf)  ←──被调用──  platform/gl/   (mupdf-gl 桌面 viewer)
                              ──  platform/x11/  (传统 X11 viewer)
                              ──  platform/java/ (JNI → Java/Android)
                              ──  platform/wasm/ (emscripten → JS/TS)
  → 每个 platform 子目录都是「把同一个核心库包成某平台/语言可用的形态」

【第三方层 thirdparty/】
  核心库的某个能力  ──委托给──  thirdparty/<库>
    字体光栅化      → freetype
    文本整形        → harfbuzz
    zlib 解压       → zlib / brotli
    JPEG 解码       → libjpeg
    JPEG2000        → openjpeg
    JBIG2           → jbig2dec
    色彩管理 ICC    → lcms2
    HTML 解析       → gumbo-parser
    Markdown        → cmark-gfm
    JS 引擎         → mujs
    OCR             → tesseract + leptonica
    条码            → zxing-cpp / zint
    网络取流        → curl
    OpenGL 窗口     → freeglut
    表格文本抽取    → extract
  → 编译后大多汇入 libmupdf-third.a
```

理解这两层后，你会明白：**`platform/` 决定「库怎么被用」，`thirdparty/` 决定「库的能力由谁支撑」**。

#### 4.3.3 源码精读

第三方依赖的权威清单是 `.gitmodules`，它逐条列出 18 个子模块：[.gitmodules:2-85](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/.gitmodules#L2-L85) 这里的每一行 `path = thirdparty/xxx` 都对应 `thirdparty/` 下一个真实目录，包括 `freetype`、`harfbuzz`、`zlib`、`libjpeg`、`openjpeg`、`jbig2dec`、`lcms2`、`mujs`、`gumbo-parser`、`cmark-gfm`、`tesseract`、`leptonica`、`curl`、`freeglut`、`brotli`、`extract`、`zxing-cpp`、`zint`。

平台层方面，WASM 绑定有自己的说明文档：[platform/wasm/README.md:1-5](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/platform/wasm/README.md#L1-L5) 把它定位为官方 `MuPDF.js` 库，用于在 JS/TS 项目中使用 MuPDF；而 Java 绑定则通过 `platform/java/` 下的 `jni/`（JNI 桥接 C 函数）与 `src/`（Java 类）协作，其入口可参见 [platform/java/mupdf_native.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/platform/java/mupdf_native.c)。

`source/helpers/` 也是值得一提的「轻量外围」：它不是格式实现，而是给库和工具提供通用辅助——`mu-threads`（跨平台线程原语，多线程渲染会用到）、`mu-office-lib`（一个更简化的办公文档封装）、`pkcs7`（数字签名相关）。它们位于 `source/` 而非 `platform/`，因为它们是平台无关的。

#### 4.3.4 代码实践

**实践目标**：为 `thirdparty/` 的 18 个子模块逐个写一句话用途说明，建立「能力 → 第三方库」的映射。

**操作步骤**：

1. 执行 `cat .gitmodules`，把 18 个 `path = thirdparty/xxx` 抄成一张清单。
2. 对每个库名，根据它的知名用途（或在该目录下读其 `README`），写一句话说明它在 MuPDF 中扮演什么角色。
3. 试着把 18 个库归类成「字体与文本 / 图像编解码 / 压缩 / HTML 与电子书 / OCR 与条码 / 网络与窗口」几组。

**需要观察的现象**：MuPDF 自身只写「文档逻辑」，几乎所有底层「硬骨头」（字形光栅化、图像解码、压缩解压）都委托给第三方库。

**预期结果**：得到一张 18 行的对照表，例如「freetype → 字体光栅化，harfbuzz → 复杂文本整形，zlib → FlateDecode 解压」。

**待本地验证**：若子模块未初始化，部分 `thirdparty/<库>` 目录可能是空的，需要先 `git submodule update --init` 才能读到各库的 README。

#### 4.3.5 小练习与答案

**练习 1**：`platform/gl/` 和 `platform/x11/` 都是 Linux 桌面 viewer，它们的差别是什么？
**参考答案**：`gl/` 是基于 OpenGL/freeglut 的现代主力 viewer `mupdf-gl`，功能最全；`x11/` 是基于 X11 的传统 viewer `mupdf-x11`，依赖少、兼容性好，但功能较少（官方称其为 legacy viewer）。

**练习 2**：为什么 `thirdparty/` 里既有 `zlib` 又有 `brotli`？它们各自对应什么场景？
**参考答案**：它们是两种不同的解压库。`zlib` 对应 PDF 的 FlateDecode 过滤器（最常见的流压缩）；`brotli` 用于较新的压缩格式（如某些 WOFF2 字体或网页压缩场景）。MuPDF 把它们都内置，以覆盖不同格式的压缩需求。

**练习 3**：`source/helpers/mu-threads` 为什么放在 `source/` 而不是 `platform/`？
**参考答案**：因为它是平台无关的跨平台线程原语（用条件编译适配各平台的线程 API），属于「核心库与工具都能用的通用基础设施」，而 `platform/` 存放的是「与某个具体平台/语言绑定的」代码。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张**完整的源码目录树地图**。

**任务**：

1. 在仓库根目录绘制一张目录树，至少覆盖以下分支，并展开到二级：
   - `include/mupdf/`（标注 `fitz.h`、`fitz/`、`pdf.h`、`pdf/`、`html.h`）
   - `source/fitz/`、`source/pdf/`、`source/tools/`、以及 `source/` 下至少两个格式目录（如 `source/xps/`、`source/html/`）
   - `platform/`（列出全部 5 个子目录）
   - `thirdparty/`（列出至少 6 个子模块）
2. 为树中每个**关键目录**写一句话用途说明，说明必须体现本讲学到的「双层架构」「平台/外围」「第三方支撑」三类视角。
3. 在地图上用三种颜色或三种标记（如 `[通用]` / `[格式]` / `[平台]` / `[依赖]`）标注每个目录属于哪一层，使得「通用层 vs 格式专用层」的边界一目了然。

**验收标准**：

- 能从地图上快速回答：「想看 PDF 对象模型的头文件去哪？」「`mutool` 的 `convert` 子命令源码在哪？」「字体光栅化由哪个第三方库负责？」
- 能指出 `pdf.h` 依赖 `fitz.h` 这一单向关系在地图上的体现。

> 这张地图建议保存下来，后续阅读任何一篇讲义时，都先在地图上定位该讲义涉及的目录，再深入代码。

## 6. 本讲小结

- MuPDF 顶层目录按关注点划分：`include/`（API 契约）、`source/`（实现）、`platform/`（viewer 与绑定）、`thirdparty/`（依赖）、`docs/`（文档）、`resources/`（内置资源）。
- 头文件分两层：通用层 `include/mupdf/fitz/`（约 50 个，格式无关）与 PDF 专用层 `include/mupdf/pdf/`（19 个，PDF 专有）。
- `fitz.h` 是通用层伞形头，用注释把子头分成 I/O、资源、设备、文档、输出等组；`pdf.h` 先 include `fitz.h` 再叠加 PDF 头，固化了「专用层依赖通用层」的单向关系。
- `source/` 的子目录与格式一一对应：`fitz/`（通用，体量最大）、`pdf/`、`xps/`、`html/`、`cbz/`、`svg/`、`reflow/`，加上 `tools/`（mutool）和 `helpers/`（线程等辅助）。
- `platform/` 提供 `gl/x11/win32/wasm/java` 五类 viewer 与绑定；`thirdparty/` 以 18 个子模块支撑字体、图像、压缩、HTML、OCR 等底层能力。
- 定位口诀：找能力看 `include/`，找实现看 `source/`，找平台看 `platform/`，找依赖看 `thirdparty/`。

## 7. 下一步学习建议

本讲建立的是「地图」。接下来建议：

- **横向巩固**：先读 [u1-l4 命令行瑞士军刀 mutool](./u1-l4-mutool-cli.md)，看 `source/tools/mutool.c` 如何把子命令分发到 `source/tools/` 下各个 `*_main` 函数，把「工具目录」的静态地图变成动态调用。
- **纵向深入通用层**：随后进入第二单元，从 [u2-l1 fz_context](./u2-l1-context.md) 开始，逐个理解 `include/mupdf/fitz/` 里那些抽象（context、document、device、pixmap…）的真实含义。
- **动手印证**：在进入下一讲前，先把综合实践的目录树地图画好，它会成为整本手册的导航底图。
