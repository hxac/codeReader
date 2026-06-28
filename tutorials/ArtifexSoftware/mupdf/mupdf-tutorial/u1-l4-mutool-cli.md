# 命令行瑞士军刀 mutool

## 1. 本讲目标

本讲带你读懂 MuPDF 的命令行入口 `mutool`。读完本讲，你应当能够：

- 说清 `mutool` 是如何用一个二进制把 `draw`、`convert`、`show`、`clean`、`merge`、`grep` 等十几个子命令路由到各自的处理函数的。
- 画出 `mutool` 的命令分发流程：`argv[0]` 名字匹配与 `argv[1]` 子名字匹配两条路径。
- 解释 `tools[]` 分发表的结构，以及 `FZ_ENABLE_JS` / `FZ_ENABLE_PDF` / `FZ_ENABLE_BARCODE` 等宏如何在编译期裁剪可用子命令。
- 亲手编译并运行 `mutool`，调用 `draw` 子命令把一页文档渲染成 PNG。

本讲是「从零认识 MuPDF」单元的第四篇，承接 u1-l2（构建系统）与 u1-l3（目录布局）。你已经知道 `make` 会产出 `mutool` 可执行文件、`source/` 下各子目录对应不同格式；本讲就钻进 `source/tools/mutool.c` 这个不到 200 行的小文件，看清「一个 `mutool` 如何变成十几个工具」。

---

## 2. 前置知识

阅读本讲前，建议你先具备以下直觉（不必精通）：

- **C 的函数指针**：`tools[]` 表里每一项都把「一个名字」和「一个 `int main(int argc, char *argv[])` 形式的函数」绑定在一起。函数指针就是这张表的核心。
- **命令行参数 `argc` / `argv`**：`argv[0]` 是程序自身的调用名（比如 `./mutool` 或 `mudraw`），`argv[1]` 才是第一个用户传入的参数。`mutool` 同时利用了这两者。
- **预处理宏 `#if` / `#endif`**：编译期裁剪。某个宏为 0 时，被它包住的表项在编译时直接消失，运行时根本不存在。
- **「多调用二进制」（multi-call binary）模式**：像 BusyBox 那样，一个二进制根据 `argv[0]` 的名字决定自己扮演哪个角色。`mutool` 也采用了这个技巧——这是 u1-l4 区别于普通「if/else 分发」的关键。

> 术语提示：本讲反复出现的「分发表（dispatch table）」，就是「一张把名字映射到函数的数组」；查到名字就调用对应函数，查不到就打印用法。这是一种避免写一长串 `if/else` 的经典 C 技巧。

---

## 3. 本讲源码地图

本讲只围绕一个主文件展开，辅以三个支撑文件：

| 文件 | 作用 |
| --- | --- |
| `source/tools/mutool.c` | **主角**。`mutool` 的 `main`，定义 `tools[]` 分发表，完成名字匹配与子命令分发。 |
| `include/mupdf/fitz/system.h` | 提供 `nelem` 宏，用于在编译期算出 `tools[]` 数组的元素个数。 |
| `include/mupdf/fitz/config.h` | 定义 `FZ_ENABLE_JS` / `FZ_ENABLE_PDF` / `FZ_ENABLE_BARCODE` 等开关的默认值，是「条件编译裁剪」的源头。 |
| `Makerules` | 把 `mujs=no`、`barcode=yes` 等 Make 变量翻译成 `-DFZ_ENABLE_*=0` 编译选项，从构建侧控制裁剪。 |
| `source/tools/mudraw.c` | `draw` 子命令的实现 `mudraw_main`，也是本讲综合实践要调用的目标；其 `usage` 字符串能帮我们确认 `-o` / `-F` 等参数。 |

> 一句话定位：`mutool.c` 负责「找谁干」，真正的「怎么干」分散在 `mudraw.c`、`muconvert.c`、`pdfshow.c` 等同目录下的兄弟文件里——每个子命令都是一个独立的 `*_main` 函数。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**`tools[]` 分发表**、**argv 名字匹配**、**条件编译裁剪**。

### 4.1 tools[] 分发表：mutool 的子命令注册表

#### 4.1.1 概念说明

`mutool` 并不是一个「大而全」的巨型函数，而是一个**调度员**。它手握一张表，表里登记了「每个子命令的名字 + 一句话说明 + 真正干活的函数」。当你敲下 `mutool draw ...`，它做的事就是：在表里查 `draw` → 找到 `mudraw_main` → 把剩下的参数原封不动交过去。

这种设计的好处是：**新增一个子命令，几乎不用动分发逻辑**——只要在表里加一行、再实现一个 `xxx_main` 函数即可。这正是 MuPDF 能在不膨胀 `mutool.c` 的前提下，长出十几个子命令的原因。

#### 4.1.2 核心流程

分发表的执行过程可以用下面这段伪代码概括：

```
main(argc, argv):
    for 每一项 entry in tools[]:
        if 名字匹配(entry.name, argv):
            return entry.func(argc, argv)   # 直接调用对应 *_main
    打印用法（遍历 tools[] 列出所有 name + desc）
    return 1
```

关键点：

- 表里每一项是 `{ 函数指针, 名字, 描述 }` 三元组。
- 命中后**直接 `return`**，把 `_main` 的返回值作为整个进程的退出码。
- 全部未命中时，再次遍历同一张表来打印用法——所以用法清单永远是和当前编译进来的子命令**保持同步**的。

#### 4.1.3 源码精读

表的元素类型定义在 `tools[]` 的声明处，是一个匿名结构体，含三个字段：函数指针 `func`、子命令名 `name`、描述 `desc`：

- [source/tools/mutool.c:59-63](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L59-L63) — 定义「函数指针 + 名字 + 描述」三元组，并开始 `tools[]` 表。

函数指针的类型是 `int (*)(int argc, char *argv[])`，和标准的 `main` 签名一致，所以每个子命令的入口都长得像一个独立程序的 `main`。

表里实际登记了哪些条目？`mutool` 用 `#if` 把不同条件下才存在的条目包起来，无条件常驻的核心条目如下：

- [source/tools/mutool.c:67-68](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L67-L68) — `draw`（渲染/转换文档）与 `convert`（带更简选项的转换）是两张「格式无关」的常驻牌。
- [source/tools/mutool.c:87-88](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L87-L88) — `grep`（全文搜索）与 `trace`（追踪 device 调用）也是常驻条目。

被 `#if FZ_ENABLE_PDF` 包起来的一大块，登记了 `audit`/`bake`/`clean`/`create`/`extract`/`info`/`merge`/`pages`/`poster`/`recolor`/`show`/`sign`/`trim` 等 PDF 专用子命令（详见 4.3 节）。

表里引用的这些 `*_main` 函数，在文件顶部先做了**前向声明**，保证 `tools[]` 引用时编译器已经知道它们的存在：

- [source/tools/mutool.c:36-57](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L36-L57) — 集中前向声明所有 `*_main` 入口（`muconvert_main`、`mudraw_main`、`murun_main`、各 `pdf*_main` 等）。

> 注意：`source/tools/` 目录里其实还有一个 `muraster.c`，但它的入口**并没有**被登记进 `tools[]`，也不在 `mutool.c` 的前向声明里。也就是说 `muraster` 不是 `mutool` 的子命令——不要被同名文件误导。本讲只讲 `tools[]` 里真实存在的条目。

最后，遍历表时用到 `nelem(tools)` 来取元素个数：

- [include/mupdf/fitz/system.h:77](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/system.h#L77) — `#define nelem(x) (sizeof(x)/sizeof((x)[0]))`，编译期算数组元素个数，所以增删条目无需手动维护计数。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读」方式验证分发表的内容，并理解「新增子命令」的成本。

**操作步骤**：

1. 打开 [source/tools/mutool.c:59-92](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L59-L92)，数一下在「默认全开」条件下，`tools[]` 里一共有多少个条目（注意区分 `#if` 包裹的条件条目与常驻条目）。
2. 为每个 `*_main` 找到它的实现文件：比如 `mudraw_main` 在 `source/tools/mudraw.c`，`pdfshow_main` 在 `source/tools/pdfshow.c`。
3. 假想你要新增一个子命令 `hello`（什么都不做，打印一句话），列出你**必须改动**的两处：① 在 `tools[]` 加一行 `{ hello_main, "hello", "say hello" }`；② 在文件顶部前向声明 `int hello_main(int argc, char *argv[]);`，再在某个 `.c` 里实现它。

**需要观察的现象**：分发逻辑（`main` 里的匹配与用法打印）**完全不需要改动**，因为它对 `tools[]` 是「数据驱动」的。

**预期结果**：你会体会到「分发表」相对 `if/else` 链的核心价值——分发代码是稳定的，新增命令是表驱动的。

#### 4.1.5 小练习与答案

**练习 1**：`tools[]` 里 `func` 字段的类型为什么写成 `int (*)(int argc, char *argv[])`，而不是直接用 `int main(...)`？

**参考答案**：因为每个子命令的入口都遵循「标准 `main`」签名，统一成函数指针类型后，整张表才能用同一种类型存放；调用时直接 `tools[i].func(argc, argv)` 即可，参数透传。

**练习 2**：如果不使用 `nelem(tools)`，而是手写一个常量 `N` 表示条目数，会带来什么维护风险？

**参考答案**：每增删一条都要记得改 `N`，一旦忘记，要么漏掉新命令、要么越界访问。`nelem` 让计数由编译器在编译期自动推导，杜绝了这种「数据与计数不同步」的 bug。

---

### 4.2 argv 名字匹配：两种分发路径

#### 4.2.1 概念说明

`mutool` 最巧妙的地方在于：它**同时支持两种调用方式**。

- **常规方式**：`mutool draw file.pdf` ——子命令 `draw` 出现在 `argv[1]`。
- **多调用方式**：把 `mutool` 二进制复制/软链成 `mudraw`，然后直接 `mudraw file.pdf` ——此时程序自己的名字（`argv[0]`）就透露了它是谁。

第二种方式就是 BusyBox 那套「一个二进制，多个身份」。它的好处是：用户和脚本可以像使用独立工具一样用 `mudraw`、`mutool draw`，而开发者只维护一份代码。

为了实现这套匹配，`mutool.c` 写了一个小巧的 `namematch` 辅助函数：判断一个字符串（如 `mudraw`）是否是 `argv[0]` 的**后缀**。

#### 4.2.2 核心流程

`main` 的分发顺序是「先 `argv[0]`，再 `argv[1]`，最后打印用法」：

```
main(argc, argv):
    if argc == 0:                         # 连程序名都没有（异常情况）
        报错 "No command name found!"

    # 路径 A：靠 argv[0] 自身的名字（多调用模式）
    去掉 argv[0] 末尾的 ".exe"（Windows）
    for entry in tools[]:
        若 argv[0] 以 "mupdf"+name、或 "pdf"+name、或 "mu"+name 结尾:
            return entry.func(argc, argv)

    # 路径 B：靠 argv[1] 显式子命令（常规模式）
    for entry in tools[]:
        若 argv[1] == entry.name:
            return entry.func(argc - 1, argv + 1)   # 注意：跳过子命令名
    若 argv[1] == "-v": 打印版本号

    # 都没命中：打印用法
    打印版本号 + 遍历 tools[] 列出所有子命令
    return 1
```

注意路径 A 与路径 B 的一个重要差别：

- **路径 A** 把「完整的 `argc, argv`」原样传给 `*_main`，因为程序名本身就是要消费的「子命令」。
- **路径 B** 调用时是 `func(argc - 1, argv + 1)`，**跳过** `argv[1]`（那个子命令名 `draw`），让 `mudraw_main` 拿到的 `argv[0]` 就像是「它自己的程序名」一样。这样无论从哪条路径进来，`*_main` 内部对参数的处理都是一致的。

#### 4.2.3 源码精读

先看后缀匹配函数 `namematch`。它判断字符串 `match` 是否恰好出现在 `argv[0]` 缓冲区 `[start, end)` 的末尾：

- [source/tools/mutool.c:94-99](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L94-L99) — `namematch`：取 `match` 长度 `len`，要求 `end-len >= start`（留得下位置），再 `strncmp` 比对末尾 `len` 个字符。

这里 `end` 指向 `argv[0]` 字符串的结尾（NUL 终止符处）。用一个简单的不等式就能描述匹配条件：设程序名长度为 \(L = end - start\)，则匹配成功当且仅当

\[
L \ge len \quad \text{且} \quad \text{name}[L-len \;..\; L) = match
\]

再看 `main` 的整体骨架与 `argc == 0` 守卫：

- [source/tools/mutool.c:117-128](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L117-L128) — `main` 入口；`argc==0` 时直接报错退出（某些奇异启动场景下 `argv[0]` 可能为空）。

**路径 A：`argv[0]` 匹配（多调用模式）。** 先把指针移到字符串末尾，Windows 下若以 `.exe` 结尾则回退 4 个字符；然后对每个工具尝试三种前缀组合：

- [source/tools/mutool.c:130-150](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L130-L150) — 遍历 `tools[]`，分别用 `mupdf<name>`、`pdf<name>`（`buf+2` 跳过前两个字符 `mu`）、`mu<name>` 去后缀匹配 `argv[0]`；命中即调用。
- [source/tools/mutool.c:137-138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L137-L138) — 去掉 `.exe` 后缀：检查末四个字符是否为 `.exe`，是则 `end = end-4`。

举个例子，工具名 `draw`：构造 `buf = "mupdfdraw"`，`buf+2 = "pdfdraw"`，再构造 `buf = "mudraw"`。于是以下程序名都能命中 `mudraw_main`：`mupdfdraw`、`pdfdraw`、`mudraw`（以及在 Windows 上的 `mudraw.exe` 等）。

**路径 B：`argv[1]` 匹配（常规模式）。** 当 `argv[0]` 没命中任何工具（比如就叫 `mutool`，没有工具叫 `tool`），就回落到显式子命令：

- [source/tools/mutool.c:152-164](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L152-L164) — 用 `strcmp` 把 `argv[1]` 与每个 `tools[i].name` 精确比对；命中则 `func(argc - 1, argv + 1)`；另外 `-v` 打印版本。

**都没命中：打印用法。** 这里复用了同一张 `tools[]`：

- [source/tools/mutool.c:166-174](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L166-L174) — 打印版本号、`usage: mutool <command> [options]`，再遍历 `tools[]` 输出每个子命令的 `name` 与 `desc`。

> 设计洞察：路径 A 用的是「后缀匹配」（`namematch`），容许前面带任意路径前缀，如 `/usr/bin/mudraw`；路径 B 用的是「精确匹配」（`strcmp`），因为子命令必须一字不差。两者的分工正是「程序名可带路径」与「子命令必须精确」这两条现实约束的体现。

#### 4.2.4 代码实践

**实践目标**：亲手验证两条分发路径都能工作。

**操作步骤**：

1. 按 u1-l2 编译出 `mutool`（默认产物在 `build/release/mutool`；若用 `make build=debug` 则在 `build/debug/mutool`）。
2. **常规路径**：运行 `mutool draw -o page1.png 你的文档.pdf 1`（`draw` 走 `argv[1]` 匹配；`1` 表示渲染第 1 页）。
3. **多调用路径**：在二进制旁建一个软链，再以新名字调用：
   ```bash
   ln -s mutool mudraw        # 在含 mutool 的目录里
   ./mudraw -o page1.png 你的文档.pdf 1
   ```
4. 故意敲一个不存在的子命令：`mutool nonsense`。

**需要观察的现象**：

- 步骤 2 与步骤 3 应当产出**完全相同**的 PNG（同一份 `mudraw_main` 在干活）。
- 步骤 4 会打印版本号 + 全部子命令的用法清单。

**预期结果**：

- 你能直观看到「`mutool draw`」与「`mudraw`」是同一个东西的两种入口。
- 用法清单的条目数，应与你数出的 `tools[]` 条目数一致。

> 若你的环境尚未编译成功，或缺少示例文档，可标注「待本地验证」——本实践的关键是理解两条路径，而非运行结果本身。

#### 4.2.5 小练习与答案

**练习 1**：为什么路径 A 用 `namematch`（后缀匹配），而路径 B 用 `strcmp`（精确匹配）？

**参考答案**：`argv[0]` 通常是带路径的完整程序名（如 `/usr/local/bin/mudraw`），需要忽略前缀只看结尾，所以用后缀匹配；`argv[1]` 是用户键入的子命令名，必须一字不差，所以用精确匹配。

**练习 2**：路径 B 调用 `tools[i].func(argc - 1, argv + 1)`，那个 `-1` 与 `+1` 的意义是什么？

**参考答案**：`argv[1]` 是子命令名（如 `draw`），对 `mudraw_main` 而言不应再出现在它看到的参数里。`argv + 1` 丢弃这一项、`argc - 1` 同步减一，于是 `mudraw_main` 拿到的 `argv[0]` 就相当于「它自己的程序名」，与从路径 A 进来时看到的世界一致。

**练习 3**：如果用户把二进制重命名为 `draw`（不带 `mu`/`mupdf` 前缀）直接运行 `./draw file.pdf`，路径 A 能命中吗？

**参考答案**：不能。路径 A 只尝试 `mupdf<name>`、`pdf<name>`、`mu<name>` 三种前缀，纯粹的 `draw` 不在其中，于是回落到路径 B；但路径 B 看 `argv[1]`，而此时 `argv[1]` 是 `file.pdf` 而非 `draw`，所以最终会打印用法、退出码 1。

---

### 4.3 条件编译裁剪：FZ_ENABLE_* 按需开关子命令

#### 4.3.1 概念说明

并非每个用户都需要全部子命令。比如嵌入式设备只要渲染（`draw`），不要 PDF 编辑（`clean`/`merge`）；又比如不集成 MuJS 引擎时，`run`（执行 JavaScript）就没有意义。MuPDF 用一组 `FZ_ENABLE_*` 宏在**编译期**决定哪些条目进入 `tools[]`。

这是 u1-l1 讲过的「`config.h` 用 `FZ_ENABLE_*` 做编译期裁剪」在命令行层面的具体体现：被裁掉的子命令不是「运行时隐藏」，而是**编译进二进制后根本不存在**——既省体积，也防止调用到没链接进去的代码。

#### 4.3.2 核心流程

裁剪由两层配合：

```
Make 变量（Makerules）
      │  翻译成
      ▼
-DFZ_ENABLE_*=0  ──►  config.h 的默认值被覆盖
      │
      ▼
mutool.c 里 #if FZ_ENABLE_* 包住的表项被预处理器删除
      │
      ▼
tools[] 只剩「未被关闭」的条目 → 运行时用法清单同步缩短
```

关键点：`config.h` 给每个开关一个**默认值（通常是 1，即开启）**；只有当构建系统通过 `-D` 显式覆盖为 0 时，对应表项才会消失。

#### 4.3.3 源码精读

在 `mutool.c` 里，`tools[]` 表被三处 `#if` 切了三刀：

- [source/tools/mutool.c:64-66](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L64-L66) — `#if FZ_ENABLE_JS` 包住 `run`（JavaScript 执行）。
- [source/tools/mutool.c:69-86](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L69-L86) — `#if FZ_ENABLE_PDF` 包住一大批 PDF 子命令（`audit`…`trim`），其中还嵌套了一层 `#ifndef NDEBUG` 控制 `cmapdump`（仅调试构建出现）。
- [source/tools/mutool.c:89-91](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L89-L91) — `#if FZ_ENABLE_BARCODE` 包住 `barcode`（条码编解码）。

这些宏的默认值来自公共头 `config.h`：

- [include/mupdf/fitz/config.h:196-198](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L196-L198) — `FZ_ENABLE_PDF` 默认为 `1`。
- [include/mupdf/fitz/config.h:264-266](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L264-L266) — `FZ_ENABLE_JS` 默认为 `1`。
- [include/mupdf/fitz/config.h:321-322](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/config.h#L321-L322) — `FZ_ENABLE_BARCODE` 默认为 `1`。

`#ifndef ... #define` 的写法意味着：**只有当命令行没传 `-D` 时，默认值才生效**；一旦构建系统传了 `-DFZ_ENABLE_JS=0`，这里的默认定义就被跳过，宏值为 0。

构建侧的「翻译」发生在 `Makerules`：它把人类友好的 Make 变量翻成 `-D` 选项：

- [Makerules:21-23](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L21-L23) — 当 `mujs=no` 时，追加 `-DFZ_ENABLE_JS=0`（于是 `run` 子命令被裁掉）。
- [Makerules:69-72](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/Makerules#L69-L72) — 只有 `barcode=yes` 才启用 zxing 并保留 `barcode` 子命令；否则追加 `-DFZ_ENABLE_BARCODE=0`。

> 把三处串起来看：用户写 `make mujs=no` → `Makerules` 产生 `-DFZ_ENABLE_JS=0` → `config.h` 的默认值被覆盖 → `mutool.c` 里 `run` 表项被预处理器删除 → 编译出的 `mutool` 不再有 `run` 子命令，用法清单也不会列出它。这就是「编译期裁剪」的全链路。

#### 4.3.4 代码实践

**实践目标**：通过阅读与构建对照，理解「裁剪」对运行时用法清单的影响。

**操作步骤**：

1. 用默认配置编译并运行 `mutool`（无参数），记下用法清单里是否包含 `run`、`barcode`。
2. 用 `make mujs=no` 重新编译（可加 `build=debug` 区分），再运行 `mutool`，对比 `run` 是否从清单里消失。
3. 在 `mutool.c` 里定位 [source/tools/mutool.c:69-86](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L69-L86)，思考：如果整个项目以 `FZ_ENABLE_PDF=0` 编译，用法清单会缩成哪几项？（答案应是 `draw`、`convert`、`grep`、`trace`，外加可能的 `run`/`barcode`。）

**需要观察的现象**：裁剪前后，用法清单**精确地**少掉被关闭的子命令——不多不少。

**预期结果**：你会确认「用法清单由 `tools[]` 数据驱动」与「`tools[]` 由 `#if` 编译期塑形」两件事叠加的效果：运行时看到的命令集合，完全由编译选项决定。

> 是否能成功重新编译取决于第三方依赖是否就位（见 u1-l2 的 `git submodule update --init`）。若不便重新编译，本实践可作为「源码阅读型实践」完成——重点是看懂三处 `#if` 与默认值的关系。运行结果标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`config.h` 里写的是 `#ifndef FZ_ENABLE_JS / #define FZ_ENABLE_JS 1 / #endif`，为什么用 `#ifndef` 而不是直接 `#define`？

**参考答案**：`#ifndef` 允许构建系统通过命令行 `-DFZ_ENABLE_JS=0` **先行定义**该宏，从而跳过这里的默认定义。若直接 `#define`，命令行的值会被无视，就无法从构建侧裁剪了。

**练习 2**：`cmapdump` 被包在 `#ifndef NDEBUG` 里，这是什么意思？

**参考答案**：`NDEBUG` 是「关闭断言」的标准宏，调试构建不定义它，于是 `#ifndef NDEBUG` 为真、`cmapdump` 进表；Release 构建通常定义 `NDEBUG`，`cmapdump` 就被裁掉。所以这是一个仅在调试构建可见的内部辅助子命令。

**练习 3**：为什么 `draw`、`convert`、`grep`、`trace` 没有被任何 `#if` 包裹？

**参考答案**：它们依赖的是 fitz 通用层（格式无关的渲染/搜索/追踪能力），不依赖 PDF 专用代码、也不依赖 JS/条码等可选第三方库，因此属于「默认常驻」的最小工具集，不参与裁剪。

---

## 5. 综合实践

把本讲三个模块串成一个完整任务：**从编译到渲染，全程追踪一次 `mutool draw` 的分发旅程。**

1. **编译**（u1-l2）：在仓库根目录执行 `make build=debug`（或默认 `make`），确认产出 `build/debug/mutool`（默认模式则在 `build/release/mutool`）。

2. **观察分发**：运行 `./build/debug/mutool`（不带任何参数），看到版本号与用法清单。对照 [source/tools/mutool.c:166-174](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L166-L174) 理解：这些行就是清单的来源。

3. **触发渲染**：准备任意一份 PDF（若仓库内无现成示例，可自行准备一份小 PDF），执行：
   ```bash
   ./build/debug/mutool draw -o page1.png 你的文档.pdf 1
   ```
   - `-o page1.png`：输出文件名（mudraw 用法见 [source/tools/mudraw.c:432](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L432)）。
   - `你的文档.pdf`：输入文件。
   - `1`：渲染第 1 页（页面号在文件之后，见 [source/tools/mudraw.c:429](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mudraw.c#L429) 的 `file [pages]`）。

4. **画出调用链**：在纸上画出这次调用的完整旅程，标注每一步对应的源码行：
   ```
   ./mutool draw -o page1.png doc.pdf 1
        │
        │ argv[0]=".../mutool" 在路径 A 不命中（无工具叫 "tool"）
        ▼
   路径 B：argv[1]="draw" 精确匹配 tools[].name (mutool.c:156-158)
        │  调用 mudraw_main(argc-1, argv+1)
        ▼
   mudraw_main 解析 -o / 文件 / 页号 (mudraw.c:2133)
        │  执行真正的渲染（这部分是 u1-l5 与第四单元的内容）
        ▼
   生成 page1.png
   ```

5. **验证多调用路径**：建一个软链 `ln -s mutool mudraw`，改用 `./mudraw -o page1.png 你的文档.pdf 1`，确认走的是路径 A（[source/tools/mutool.c:139-149](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutool.c#L139-L149)），结果与步骤 3 完全一致。

**验收标准**：你能讲清楚「敲下 `mutool draw` 之后，控制流在哪一行离开了 `mutool.c`、进入了 `mudraw_main`」，并且用法清单与 `tools[]` 条目一一对应。

> 提示：渲染部分（`mudraw_main` 内部如何打开文档、构造矩阵、渲染像素）不在本讲范围——那是 u1-l5「第一个渲染程序」和第四单元「设备模型与渲染管线」的主题。本讲只负责到「分发」为止。

---

## 6. 本讲小结

- `mutool` 是一个**调度器**：核心是一张 `tools[]` 分发表，每项是 `{ 函数指针, 名字, 描述 }` 三元组，命中即调用对应 `*_main`。
- 分发有**两条路径**：路径 A 用 `namematch` 后缀匹配 `argv[0]`，支持「一个二进制多个身份」（如 `mudraw`）；路径 B 用 `strcmp` 精确匹配 `argv[1]` 子命令（如 `mutool draw`），且调用时跳过子命令名。
- 用法清单由**同一张 `tools[]` 数据驱动**，所以「能看到的命令」永远等于「编译进来的命令」。
- `FZ_ENABLE_JS` / `FZ_ENABLE_PDF` / `FZ_ENABLE_BARCODE` 等 `#if` 在**编译期**裁剪表项；`config.h` 给默认值，`Makerules` 把 Make 变量翻成 `-D` 选项完成覆盖。
- `nelem` 宏让表元素个数在编译期自动推导，增删条目无需手动维护计数。
- 新增子命令只需「加一行表项 + 前向声明 + 实现 `*_main`」，分发逻辑无需改动——这就是表驱动分发相对于 `if/else` 链的价值。

---

## 7. 下一步学习建议

本讲你只看到了「`mutool` 如何把控制权交给 `mudraw_main`」，但还没进到 `mudraw_main` 内部。建议按以下顺序继续：

1. **u1-l5「第一个渲染程序：跑通 example.c」**：跳出 `mutool` 的命令行包装，用最精简的库 API（`fz_new_context` → `fz_register_document_handlers` → `fz_open_document` → `fz_new_pixmap_from_page_number`）亲手渲染一页，看清渲染的最小骨架。
2. **第二单元「fitz 核心基石」**：理解 `fz_context` 为什么是几乎所有调用的第一个参数，这是读懂 `mudraw_main` 内部的前提。
3. **第四单元「设备模型与渲染管线」**：把 `mudraw_main` 里出现的 `fz_run_page`、display-list、draw device 串成完整的渲染管线——那时你会回过头来感谢本讲打下的「分发」基础。

> 延伸阅读：可以扫一眼 `source/tools/` 下任意一个 `pdf*_main`（例如 `pdfshow.c` 的 `pdfshow_main`），观察它们都是「标准 `main` 签名 + 自己解析参数」的独立小程序——这正是 `tools[]` 能把它们一视同仁地装进同一张表的根本原因。
