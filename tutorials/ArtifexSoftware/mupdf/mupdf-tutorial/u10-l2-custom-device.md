# 实现自定义 device

## 1. 本讲目标

本讲是「交互、扩展与二次开发」单元的第二篇。在 u4-l1 里我们已经知道 `fz_device` 是一张函数指针虚表，扮演「访客/消费者」的角色，把格式解释器发出的统一绘图指令分流到不同后端。本讲要往前走一步：**自己动手写一个 device**。

学完本讲，你应当能够：

- 说出实现一个自定义 device 需要填充哪些虚表回调、哪些可省略。
- 看懂 `trace-device.c` 与 `list-device.c` 这两个对照范例的结构，并能区分「借用资源」与「持有资源」两种 device 的写法差异。
- 独立写出一个最小可运行的 device，挂到 `fz_run_page` 上拦截 `fill_path`/`fill_text` 等指令，完成内容统计、调试输出或自定义导出。

## 2. 前置知识

本讲默认你已经掌握 u4-l1 的以下结论（这里只做最简回顾，不展开）：

- **device 是虚表**：`struct fz_device` 内部几乎全是函数指针，每个指针对应一类绘图指令（填色路径、描边文字、裁剪、图片……）。
- **生产者/消费者解耦**：PDF、XPS 等格式解释器（生产者）只管调用 `fz_fill_path(ctx, dev, ...)` 这类**包装函数**；包装函数内部做判空、异常隔离，再转发到 `dev->fill_path`（消费者回调）。所以新增一个后端 = 写一组回调 + 填表，**不用改任何格式解释器**。
- **生命周期四步**：`new → run → close → drop`。`close` 冲刷缓冲、`drop` 释放资源；忘记 `close` 直接 `drop` 会触发 `dropping unclosed device` 警告（前提是该 device 定义了 `close_device`）。
- **容器回调与 scissor 栈**：`clip_*`/`begin_mask`/`begin_group`/`begin_tile` 必须与 `pop_clip`/`end_*` 配对，框架在包装层帮你维护逐层取交的裁剪栈。

还需要一点 C 基础：「结构体首成员多态」——把基类 `fz_device` 作为派生结构体的第一个字段，于是指向派生结构体的指针可以安全地转型为 `fz_device *`，反之亦然。这是 MuPDF 在 C 里手写继承的核心手法（u6-l1 的 document writer 也用了同一招）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/device.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h) | device 的「契约」：`struct fz_device` 虚表定义、`fz_new_derived_device` 宏、包装函数声明、close/drop 语义。 |
| [source/fitz/device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c) | device 的「运行时」：`fz_new_device_of_size` 分配、包装函数的判空与异常隔离、`fz_disable_device` 出错自禁用、close/drop 实现。 |
| [source/fitz/trace-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c) | 范例一：把每条指令打印成带缩进的 XML，**只借用**一个 `fz_output`，不持有任何资源。 |
| [source/fitz/list-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c) | 范例二：把指令录制成显示列表，**持有** display list / colorspace 等引用，因此需要 `drop_device`。 |
| [source/tools/mutrace.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c) | `mutool trace` 工具：真实程序如何创建 trace device 并用 `fz_run_page` 驱动它。 |
| [include/mupdf/fitz/text.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/text.h) | `fz_text` / `fz_text_span` / `fz_text_item` 的结构，统计字符数时要用到。 |

## 4. 核心概念与源码讲解

### 4.1 device 虚表填充：从结构体到构造函数

#### 4.1.1 概念说明

「实现一个 device」在 MuPDF 里被刻意设计得非常轻量：你**不需要注册**、**不需要改框架**，只要做三件事——

1. 定义一个**派生结构体**，把 `fz_device` 作为第一个字段（习惯上起名 `super`）。
2. 实现 you 关心的若干回调函数，签名**严格照抄** `struct fz_device` 里对应函数指针的类型。
3. 写一个构造函数，用 `fz_new_derived_device(ctx, 你的类型)` 分配并清零内存，再把回调逐个赋给 `dev->super.xxx`。

为什么这么简单？因为 device.h 的作者一开始就把 `struct fz_device` 设计成**公开结构体**，并明确鼓励外部实现。device.h 顶部那段注释把意图说得直白：

> The device structure is public to allow devices to be implemented outside of fitz.
> （device 结构体之所以公开，正是为了让 device 可以在 fitz 之外实现。）

也就是说，写自定义 device 是被官方支持的**一等扩展点**，不是 hack。

#### 4.1.2 核心流程

一个最小 device 的诞生流程：

```
fz_new_derived_device(ctx, MyDevice)
        │
        │  展开为宏：分配 sizeof(MyDevice) 字节、calloc 清零、打 Memento 标签
        ▼
返回 MyDevice*（其首成员即 fz_device，refs 已被设为 1）
        │
        │  填充虚表：dev->super.fill_path = my_fill_path; …
        │  初始化派生字段
        ▼
强转为 fz_device* 返回给调用方
        │
        │  调用方：fz_run_page(ctx, page, dev, ctm, NULL)
        ▼
框架的包装函数 fz_fill_path / fz_fill_text / … 逐条转发到你的回调
        │
        │  渲染结束：fz_close_device(ctx, dev); fz_drop_device(ctx, dev);
```

两个关键认知：

- **只填你关心的回调，其余保持 NULL**。包装函数对每个回调都做 `if (dev->xxx)` 判空（见 4.3.3），NULL 表示「本 device 不处理这类指令」，直接跳过。这就是为什么 trace device 能只实现绘图回调、对不关心的元信息回调留空。
- **`refs` 由框架管**。`fz_new_device_of_size` 把 `refs` 初始化为 1，`fz_drop_device` 用引用计数决定何时真正释放。你**不要**手动改 `refs`。

#### 4.1.3 源码精读

先看分配与计数的根。所有 device 最终都经 `fz_new_device_of_size` 创建：

[device.c:27-33](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L27-L33) —— 用 `fz_calloc` 分配并**整块清零**，再把 `refs` 设为 1。注意「清零」这一步至关重要：它保证你**没显式赋值**的虚表槽全是 NULL，从而安全地被包装函数跳过。派生结构体里没初始化的字段也一并是 0。

我们一般不直接调 `fz_new_device_of_size`，而是用宏：

[device.h:388-390](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L388-L390) —— `fz_new_derived_device(ctx, TYPE)` 展开后，会以 `sizeof(TYPE)` 调用上面那个函数、用 `Memento_label` 打上类型名字符串（方便内存调试时辨认），再返回 `TYPE*`。这就是「派生结构体首成员多态」的落地：分配的是 `TYPE` 大小，但首成员就是 `fz_device`，所以指针可以双向转型。

再看你要照抄签名的虚表本体：

[device.h:289-344](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L289-L344) —— `struct fz_device` 的完整定义。开头三个字段 `refs`/`hints`/`flags` 由框架管理；接下来是两组回调：`close_device`/`drop_device`（生命周期），然后是按「路径 / 文字 / 着色与图像 / 容器 / 图层与结构 / 元文本」分组的一大片绘图回调；最后是框架自用的容器栈字段 `container_len`/`container_cap`/`container` 和 `passthrough`。

填表时最容易抄错的是回调签名。以本讲实践要用的两个为例，原样照抄即可：

- `fill_path`：

  [device.h:298](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L298) ——
  `void (*fill_path)(fz_context *, fz_device *, const fz_path *, int even_odd, fz_matrix, fz_colorspace *, const float *color, float alpha, fz_color_params);`

- `fill_text`：

  [device.h:303](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L303) ——
  `void (*fill_text)(fz_context *, fz_device *, const fz_text *, fz_matrix, fz_colorspace *, const float *color, float alpha, fz_color_params);`

> 提示：所有回调的第二个参数都是 `fz_device *dev`（而不是你的派生类型），所以在回调内部第一行通常是 `MyDevice *dev = (MyDevice *)dev_;` 做一次向下转型。这是 C 手写多态的标准开销。

#### 4.1.4 代码实践

**实践目标**：不写可运行代码，先在脑中走通「分配—清零—填表」三步，验证你理解了派生结构体与虚表的关系。

**操作步骤**：

1. 阅读 [device.c:27-33](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L27-L33)，确认 `fz_calloc` 把整块内存清零。
2. 假设你要写一个「统计 device」，请你在纸上写出它的派生结构体声明，要求：以 `fz_device super` 起头，再带两个整型计数器 `path_count`、`char_count`。
3. 对照 [device.h:289-298](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L289-L298)，回答：如果你**只**给 `super.fill_path` 赋了值、其余槽都没碰，那么 `fz_stroke_text` 这类指令到来时会发生什么？

**需要观察的现象 / 预期结果**：

- 第 2 步的结构体应当长这样（示例代码，非项目原有）：

  ```c
  typedef struct {
      fz_device super;     /* 必须是首成员 */
      int path_count;
      int char_count;
  } fz_stats_device;
  ```

- 第 3 步：因为 `fz_calloc` 已把 `super.stroke_text` 清成 NULL，包装函数 `fz_stroke_text` 里的 `if (dev && dev->stroke_text)` 判空失败，**直接跳过、什么都不做**，也不会报错。这正是「只填关心的回调」能成立的根本原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fz_new_derived_device` 返回的指针可以安全地当成 `fz_device *` 用？

**参考答案**：因为派生结构体把 `fz_device super` 放在**第一个字段**，C 标准保证结构体首成员的地址等于结构体自身的地址，所以 `MyDevice*` 与指向其 `super` 的 `fz_device*` 在二进制上是同一个地址，双向转型安全。

**练习 2**：如果你的 device 在回调里 `fz_malloc` 了一些内存，挂在派生字段上，你应该在哪里释放？

**参考答案**：实现 `drop_device` 回调，在其中释放自定义内存。`fz_drop_device` 在引用计数归零时会调用 `dev->drop_device`（见 4.2.3 的 list 范例）。框架自己只负责 `fz_free(ctx, dev->container)` 和 `fz_free(ctx, dev)`，不会动你的派生字段。

---

### 4.2 trace 与 list：两个对照范例

#### 4.2.1 概念说明

仓库里现成的 device 实现就是最好的教材。我们挑两个极端对照着看：

- **trace device**（[trace-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c)）：把每条指令格式化成一行 XML 写到 `fz_output`。它**不拥有**任何资源——`out` 是调用方传进来借给它用的——所以它**不实现** `close_device`/`drop_device`。
- **list device**（[list-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c)）：把指令录制成显示列表。它在构造时 `fz_keep_display_list` / `fz_keep_colorspace` **持有了引用**，所以它**必须**实现 `drop_device` 来配对释放。

一句话总结这两个范例教给我们的判据：**「你的 device 是否 keep 了别人的对象？」是——就需要 `drop_device`；否——就可以省略。** 这是写自定义 device 时最容易漏掉的资源管理决策。

#### 4.2.2 核心流程

两个范例的构造函数骨架几乎一样，差别只在「是否填 `drop_device`」和「是否初始化资源字段」：

```
trace device 构造                    list device 构造
─────────────────                    ─────────────────
fz_new_derived_device(...)           fz_new_derived_device(...)
填 fill_path / fill_text / …         填 fill_path / fill_text / …
（不填 drop_device）                  填 drop_device = fz_list_drop_device
dev->out = out;        ← 借用        dev->list = fz_keep_display_list(...)  ← 持有
return (fz_device*)dev;              return &dev->super;
```

trace device 还有一个值得学的「纯装饰」技巧：它维护一个 `depth` 整数，每进入一个容器（`clip_path`/`begin_group`/…）就 `depth++`，每 `pop_clip`/`end_*` 就 `depth--`，用 `depth` 控制缩进空格数，让输出的 XML 带有可读的层级缩进。注意：这个 `depth` 是 trace device **自己**的排版计数器，**和**框架维护的容器 scissor 栈（`dev->container`）**是两回事**——框架的栈由包装函数自动管，你写 device 时通常不用操心。

#### 4.2.3 源码精读

**trace device 的派生结构体**：

[trace-device.c:25-30](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L25-L30) —— 只有三个字段：`fz_device super`（基类）、`fz_output *out`（借来的输出）、`int depth`（缩进层级）。极简。

**trace device 的构造函数**（本节最重要的范本）：

[trace-device.c:664-709](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L664-L709) —— 注意几件事：

1. 第 666 行 `fz_new_derived_device(ctx, fz_trace_device)` 分配并清零。
2. 第 668–704 行把**几十个**回调逐个赋给 `dev->super.xxx`——这就是「填虚表」的现场。trace 几乎填满了所有绘图回调，因为它想记录一切。
3. **没有**任何一行给 `close_device` 或 `drop_device` 赋值——因为它不持有资源，借来的 `out` 由调用方负责。
4. 第 706 行 `dev->out = out;` 记下借来的输出，第 708 行强转返回。

挑两个回调看 trace 的写法（也是你写统计 device 时要模仿的形式）：

- `fill_path`：[trace-device.c:178-197](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L178-L197) —— 先把 `dev_` 转型回 `fz_trace_device*`，取出 `out`，按 `depth` 缩进，打印 `<fill_path …>`，再调 `fz_trace_path` 把路径细节展开。
- `fill_text`：[trace-device.c:276-291](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L276-L291) —— 同样的转型套路，打印 `<fill_text …>` 后调 `fz_trace_text`。

`fz_trace_text` 内部遍历 `fz_text` 的 span 链表（[trace-device.c:118-124](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L118-L124)），对每个 span 又遍历其 `items[]`（[trace-device.c:80-113](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L80-L113)）。**这段 span 遍历就是综合实践里统计字符数要照抄的模板。**

再看 `depth` 是怎么配对的：进入 `clip_path` 时 `dev->depth++`（[trace-device.c:255](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L255)），对应的 `pop_clip` 里 `dev->depth--`（[trace-device.c:473-481](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L473-L481)）。缩进空格由 `fz_trace_indent` 按 `depth` 生成（[trace-device.c:32-36](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L32-L36)）。

**list device 的派生结构体**（对照看「持有资源」的样子）：

[list-device.c:156-177](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L156-L177) —— 以 `fz_device super` 起头，后面挂了一串**需要管理生命周期**的字段：`fz_display_list *list`、当前缓存的 `path`/`stroke`/`colorspace`、颜色数组、以及一个手写的容器 `stack[STACK_SIZE]`。字段多，是因为录制显示列表需要暂存「当前指令」的各组成部分。

**list device 的构造函数**：

[list-device.c:1490-1548](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1490-L1548) —— 骨架与 trace 一样（`fz_new_derived_device` + 逐个填表），但有三个关键不同：

1. 第 1535 行 `dev->super.drop_device = fz_list_drop_device;` —— **填了 drop**，因为它持有引用。
2. 第 1537 行 `dev->list = fz_keep_display_list(ctx, list);` —— 用 `fz_keep_*` **增加引用计数**来持有 list。
3. 第 1542 行 `dev->colorspace = fz_keep_colorspace(ctx, fz_device_gray(ctx));` —— 同样 keep 了一个默认颜色空间。

**list device 的 drop**：

[list-device.c:1479-1488](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/list-device.c#L1479-L1488) —— 逆序 drop 掉构造时 keep 的所有对象（colorspace、stroke、path、display_list）。每个 `fz_keep_*` 都必须配对一个 `fz_drop_*`，这正是 u2-l2 引用计数铁律在 device 上的体现。注意：drop 里**只**释放派生字段持有的对象，**不要** `fz_free(dev)` 本身——框架的 `fz_drop_device` 会在调用完 `drop_device` 之后负责释放 device 结构体本身（见 [device.c:92-104](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L92-L104)）。

#### 4.2.4 代码实践

**实践目标**：用现成的 `mutool trace` 亲眼看到 trace device 的输出，建立「回调 ↔ XML 行」的直觉，为后面自己写 device 做铺垫。

**操作步骤**：

1. 确认 `mutool` 已编译（u1-l2），并准备一个简单的 PDF（哪怕只有一页文字 + 一个矩形）。
2. 运行：

   ```bash
   ./build/release/mutool trace -d 你的文件.pdf 1
   ```

   `-d` 表示先用显示列表录制再回放（见 [mutrace.c:77-81](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c#L77-L81)）；不加 `-d` 则直接对原页跑 trace（[mutrace.c:83-85](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c#L83-L85)）。

**需要观察的现象 / 预期结果**：

- 输出是一段 XML：外层 `<page>`，里面是带缩进的 `<fill_path>`、`<fill_text>`、`<clip_path>` … `<pop_clip/>` 等标签。
- 留意缩进层级：每出现一个 `<clip_path>`/`<group>`，其内部指令的缩进会比外层多 4 个空格；遇到对应的 `<pop_clip/>`/`</group>` 缩进再回到原级。这正好对应 4.2.3 里 `depth++`/`depth--` 的配对。
- 如果某页只有图片没有文字，你将**看不到**任何 `<fill_text>`——印证了「回调只在对应指令出现时才被调用」。

> 说明：若当前环境没有可用的示例 PDF，本步骤标注为「待本地验证」；你也可以直接阅读 trace-device.c 里各 `fz_trace_*` 函数打印的标签字符串来推断输出形态。

#### 4.2.5 小练习与答案

**练习 1**：trace device 为什么不需要 `drop_device`，而 list device 需要？用一句话回答。

**参考答案**：判据是「是否 `fz_keep_*` 了别人的对象」。trace 只借用 `out`，没有增加任何引用计数，故无需 drop；list 在构造时 keep 了 display list 和 colorspace，引用计数加了就要在 drop 里减回去。

**练习 2**：如果模仿 trace 写一个 device，构造时把传入的 `fz_output *out` **存进**派生字段，但**没有**对它 `fz_keep_output`，那么这个 device 该不该实现 `drop_device` 来 `fz_drop_output(out)`？

**参考答案**：**不该**。没有 keep 就没有「归我所有」的引用，`out` 的生命周期归调用方管。如果 Dropage 里多此一举地 drop，会与调用方的 drop 重复释放，造成引用计数失衡（这正是 trace device 的做法：它只存 `out`，不在 drop 里动它）。

---

### 4.3 自定义 device 的应用场景与设计要点

#### 4.3.1 概念说明

什么情况下值得自己写一个 device？只要你的需求是「**我想在页面内容被解释的过程中，对每一条绘图指令做点自定义处理**」，就适合用 device，而不必去改格式解释器。典型场景：

- **内容分析与统计**：数一页有多少条路径、多少个字形、用了哪些颜色空间——本讲综合实践就是这类。
- **调试与可观测**：trace device 本身就是官方的调试器，把指令流可视化；你也可以写一个只打印 `fill_text` 的 device 来排查「为什么这段文字没渲染出来」。
- **自定义导出**：把指令翻译成另一种矢量格式或中间表示（SVG device、docx device 都是这条路，见 u6-l3）。本质上 document writer 的 `begin_page` 返回的就是一个 device（u6-l1）。
- **过滤与裁剪**：用一个 passthrough device 包住真实 device，拦截部分指令、放行其余（culling device、test device 即此模式，见 [device.h:596-605](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L596-L605)）。

device.h 顶部的注释明确把这种开放性写进了设计意图：

[device.h:35-45](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L35-L45) —— 列举了 trace/draw/list/text/bbox 等 device 后，写道「Other devices can (and will) be written in the future.」（其他 device 可以、也将会在未来被编写出来）。

#### 4.3.2 核心流程

写自定义 device 时的几条设计要点，串起来就是一套检查清单：

1. **决定关心的回调**：只实现你需要的，其余留空（calloc 已清零）。统计 device 通常只需 `fill_path`/`fill_text`/`fill_image` 这几个 marking 回调。
2. **决定是否持有资源**：若构造里 `fz_keep_*` 了对象，就必须实现 `drop_device` 配对 `fz_drop_*`；若纯借用或只统计计数，则可省略。
3. **决定是否需要 `close_device`**：只有当你的 device 有「缓冲」需要冲刷时才需要（比如 list device 录制完要收尾）。纯统计 device 不需要。
4. **回调里允许抛异常吗？** 允许，但要小心：包装函数捕获到回调抛出的异常后，会**清空整张虚表**（出错自禁用，见 4.3.3），之后该 device 对任何指令都变成 no-op，并把异常向上 rethrow。所以「device 出错后即报废」是有意为之的安全策略。
5. **让框架帮你管容器栈**：除非你要像 trace 那样自己跟踪缩进，否则**不要**自己维护 clip/group 的嵌套——包装函数 `fz_clip_path`/`fz_begin_group` 已经在调用你的回调**之前**把 scissor 压栈了（见 4.3.3）。你只要保证「容器类回调与 end/pop 回调成对实现」即可。
6. **驱动方式与普通 device 一致**：用 `fz_run_page(ctx, page, dev, ctm, NULL)` 或对显示列表用 `fz_run_display_list(...)`，结束时 `fz_close_device` + `fz_drop_device`。

#### 4.3.3 源码精读

**包装函数如何调用你的回调**（这是你的 device 能「被动」收到指令的根本原因）：

[device.c:246-260](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L246-L260) —— `fz_fill_text` 的全部实现：`if (dev && dev->fill_text)` 判空，在 `fz_try` 内调用 `dev->fill_text(...)`；一旦 `fz_catch` 捕获到异常，立即 `fz_disable_device` 并 `fz_rethrow`。**每个**包装函数都是这个套路，所以你的回调天然被异常隔离保护。

**出错自禁用**（理解 device 的容错模型）：

[device.c:35-67](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L35-L67) —— `fz_disable_device` 把**所有**回调指针（含 close/drop 之外的绘图回调）置 NULL。后果是：一旦某个回调抛了异常，这个 device 就再也收不到任何指令——它「自残」成了空设备，避免在异常状态下继续产生半成品输出。你的自定义回调因此不必处处防御：抛了就抛了，框架会兜底。

**框架帮你压 scissor 栈**（解释为何你通常不用自己管容器）：

[device.c:199-220](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L199-L220) —— `fz_clip_path` 在调用 `dev->clip_path` **之前**，先算路径包围盒、与传入 scissor 取交、`push_clip_stack` 压栈。也就是说，容器嵌套的几何追踪是包装层的责任；你写 device 时若只关心 marking 指令，完全可以忽略这套栈。

**close 与 drop 的语义**（决定你要不要实现这两个回调）：

[device.c:69-84](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L69-L84) —— `fz_close_device` 在 `fz_try` 内调 `dev->close_device`，并在 `fz_always` 里**无条件** `fz_disable_device`（所以 close 之后 device 即不可用，即使你忘了它也不会重复执行）。

[device.c:92-104](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L92-L104) —— `fz_drop_device` 引用计数归零时：若 `close_device` 仍非 NULL（说明没 close 过），发 `dropping unclosed device` **警告**；然后调 `dev->drop_device`（若有）；最后框架自己 `fz_free(container)` 和 `fz_free(dev)`。**注意**：trace/list 都没设 `close_device`，所以它们不 close 直接 drop 也不会触发警告——这也提示我们：纯只读/只统计的 device 通常连 close 都不用实现。

**真实程序如何驱动一个 device**（综合实践的模板来源）：

[mutrace.c:60-96](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c#L60-L96) —— `mutool trace` 的 `runpage`：`fz_load_page` → `fz_new_trace_device(ctx, fz_stdout(ctx))`（第 76 行）→ `fz_run_page(ctx, page, dev, fz_identity, NULL)`（第 84 行）→ `fz_always` 里 `fz_drop_device`。你写统计 device 时，把 `fz_new_trace_device` 换成你的 `fz_new_stats_device`，其余一字不改。

#### 4.3.4 代码实践

**实践目标**：阅读理解题——把 4.3.3 的三段源码串起来，解释「device 出错后会怎样」。

**操作步骤**：

1. 假设你的统计 device 的 `fill_text` 回调里，因为某个损坏 PDF 导致 `span` 为非 NULL 但 `span->items` 为 NULL，你的循环解引用崩溃并抛出异常。
2. 对照 [device.c:246-260](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L246-L260) 与 [device.c:35-67](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/device.c#L35-L67)，描述从异常发生到 `fz_run_page` 返回之间，框架替你做了什么。

**需要观察的现象 / 预期结果**：`fz_fill_text` 的 `fz_catch` 捕获异常 → 调 `fz_disable_device` 把你 device 的全部回调清空 → `fz_rethrow` 把同一个异常抛给 `fz_run_page` 的调用者。此后即便页面还有更多文字，你的 device 也不会再收到任何 `fill_text`（因为回调已被置 NULL）。这就是「device 出错即报废」机制——它保证了不会在一个已经出错的设备上继续累积垃圾结果。

#### 4.3.5 小练习与答案

**练习 1**：你想要一个 device，它把页面里**所有文字**原样忽略、只统计图片数量。该实现哪些回调？

**参考答案**：实现 `fill_image`（图片计数 +1）即可；文字相关的 `fill_text`/`stroke_text` 等留空（NULL）——框架判空后会自动跳过，相当于「忽略」。注意 PDF 里有时文字以 `clip_stroke_text` 等形式出现，若要彻底忽略文本，对照 trace 填表（[trace-device.c:673-677](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/trace-device.c#L673-L677)）把所有 `*_text` 槽都留空。

**练习 2**：passthrough device（[device.h:402-404](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L402-L404)）相比从零写一个 device，适合什么场景？

**参考答案**：当你**只想拦截少数几类指令、其余照常渲染**时（比如只裁掉某些字形、只检测是否有彩色），用 passthrough device 包住一个真实 device（如 draw device），默认所有调用都透传给底层，你只 override 感兴趣的那几个回调。从零写则要自己重新实现全部需要的功能，适合「全新后端」（如全新导出格式）。

---

## 5. 综合实践

**任务**：参照 trace-device.c，从零实现一个「统计 device」——在 `fill_path` 时累计路径数、在 `fill_text` 时累计字形数，渲染一页后打印这两个统计值。这会把本讲的「派生结构体 + 填虚表 + 回调签名 + 驱动方式」一次性串起来。

### 5.1 设计决策（先想清楚再写代码）

对照 4.3.2 的检查清单：

- **关心的回调**：只 `fill_path` 和 `fill_text`，其余留空。
- **是否持有资源**：只统计两个整数，不 keep 任何对象 → **不需要** `drop_device`。
- **是否需要 close**：没有缓冲要冲刷 → **不需要** `close_device`。
- **结果怎么传回调用方**：参考 bbox device「传入指针、device 往里写」的模式（[device.h:524](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L524)），让调用方传入两个 `int*`，device 把计数写回。

### 5.2 统计 device 的实现（示例代码，非项目原有）

把下面这段保存为 `stats-device.c`（这是为教学编写的示例，不在仓库中）：

```c
/* stats-device.c —— 示例代码：统计 fill_path 次数与 fill_text 字形数 */
#include "mupdf/fitz.h"

/* 1) 派生结构体：fz_device 必须是首成员 */
typedef struct
{
    fz_device super;
    int *path_count;   /* 调用方提供，结果写回这里 */
    int *char_count;
} fz_stats_device;

/* 2) fill_path 回调：签名照抄 device.h:298 */
static void
stats_fill_path(fz_context *ctx, fz_device *dev_, const fz_path *path,
    int even_odd, fz_matrix ctm, fz_colorspace *cs,
    const float *color, float alpha, fz_color_params cp)
{
    fz_stats_device *dev = (fz_stats_device *)dev_;   /* 向下转型 */
    (*dev->path_count)++;
}

/* 3) fill_text 回调：签名照抄 device.h:303；遍历 span 照抄 trace-device.c:118-124 */
static void
stats_fill_text(fz_context *ctx, fz_device *dev_, const fz_text *text,
    fz_matrix ctm, fz_colorspace *cs,
    const float *color, float alpha, fz_color_params cp)
{
    fz_stats_device *dev = (fz_stats_device *)dev_;
    fz_text_span *span;
    for (span = text->head; span; span = span->next)
        *dev->char_count += span->len;   /* 每个 item 约等于一个字形 */
}

/* 4) 构造函数：分配清零 + 填虚表（只填两个槽） */
fz_device *
fz_new_stats_device(fz_context *ctx, int *path_count, int *char_count)
{
    fz_stats_device *dev = fz_new_derived_device(ctx, fz_stats_device);

    dev->super.fill_path = stats_fill_path;
    dev->super.fill_text = stats_fill_text;
    /* 其余回调保持 NULL：calloc 已清零，包装函数会自动跳过 */

    dev->path_count = path_count;
    dev->char_count = char_count;
    *path_count = 0;
    *char_count = 0;

    return (fz_device *)dev;
}
```

关于「字符数」的精度说明：`span->len` 是 span 内 `fz_text_item` 的个数（[text.h:76](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/text.h#L76)），严格说是「字形数」而非「Unicode 字符数」——因为连字（ligature）等情况下会出现「一个字符多个字形」或反过来（`item.ucs == -1`，见 [text.h:44-51](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/text.h#L44-L51)）。本实践用字形数已足够；若要更接近「字符数」，可改为只累加 `span->items[i].ucs >= 0` 的条目。

### 5.3 驱动主程序（示例代码，模仿 mutrace.c:60-96）

```c
/* stats-main.c —— 示例代码：打开文档，对第 1 页跑统计 device */
#include "mupdf/fitz.h"
#include <stdio.h>

/* 声明上面定义的构造函数 */
fz_device *fz_new_stats_device(fz_context *ctx, int *path_count, int *char_count);

int main(int argc, char **argv)
{
    fz_context *ctx;
    fz_document *doc = NULL;
    fz_page *page = NULL;
    fz_device *dev = NULL;
    int paths = 0, chars = 0;

    if (argc < 2) { fprintf(stderr, "usage: %s file.pdf\n", argv[0]); return 1; }

    ctx = fz_new_context(NULL, NULL, FZ_STORE_UNLIMITED);
    fz_register_document_handlers(ctx);

    fz_var(doc); fz_var(page); fz_var(dev);   /* u2-l3：跨 fz_try 必须 fz_var */
    fz_try(ctx)
    {
        doc = fz_open_document(ctx, argv[1]);
        page = fz_load_page(ctx, doc, 0);          /* 第 1 页，内部下标 0 */
        dev = fz_new_stats_device(ctx, &paths, &chars);
        fz_run_page(ctx, page, dev, fz_identity, NULL);
        fz_close_device(ctx, dev);                 /* 统计 device 无 close，调用也无副作用 */
        printf("fill_path 次数 = %d\n", paths);
        printf("字形个数     = %d\n", chars);
    }
    fz_always(ctx)
    {
        fz_drop_device(ctx, dev);    /* 释放顺序：device → page → doc → ctx */
        fz_drop_page(ctx, page);
        fz_drop_document(ctx, doc);
    }
    fz_catch(ctx)
    {
        fz_report_error(ctx);
        fprintf(stderr, "rendering failed\n");
    }

    fz_drop_context(ctx);
    return 0;
}
```

### 5.4 操作步骤

1. 按 u1-l2 的构建体系把 `stats-device.c`、`stats-main.c` 与 `libmupdf` 一起编译（示例命令，待本地验证）：

   ```bash
   cc -Iinclude stats-device.c stats-main.c -Lbuild/release -lmupdf -lmupdf-third \
      -lm -lpthread -o stats
   ```

2. 准备一个测试 PDF（含若干矩形/线条和一段文字）。
3. 运行 `./stats 你的文件.pdf`。

### 5.5 需要观察的现象 / 预期结果

- 终端打印两个非零整数：路径数应大致等于页面里「填充图形」的个数，字形数应大致等于可见文字的字形总数。
- **对照验证**：对同一个 PDF 跑 `mutool trace -d 你的文件.pdf 1`，数一数输出里 `<fill_path>` 标签的数量，应与你的 `fill_path 次数` 一致；`<fill_text>` 内 `<g .../>`（单字形）的数量之和应与你的 `字形个数` 接近。若两者对得上，说明你的 device 正确地挂进了渲染管线。
- **进阶实验**：把 `fz_run_page` 换成「先 `fz_new_display_list_from_page` 录制、再 `fz_run_display_list` 回放」（参考 [mutrace.c:77-81](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/tools/mutrace.c#L77-L81)），统计结果应不变——因为显示列表回放发出的指令与直接解释页面一致。

> 说明：精确的计数值取决于具体 PDF，故具体数字标注为「待本地验证」；判定标准是「自写 device 与 `mutool trace` 输出互相印证」。

## 6. 本讲小结

- 实现自定义 device = **定义派生结构体（`fz_device super` 起头）+ 实现关心的回调（签名照抄 `struct fz_device`）+ 用 `fz_new_derived_device` 分配并填虚表**；其余槽保持 NULL，包装函数会自动跳过。
- **是否需要 `drop_device` 的判据**：构造时 `fz_keep_*` 了对象就需要（如 list device），纯借用或只统计就不需要（如 trace device）；`close_device` 仅在有缓冲需冲刷时才需要。
- 回调里**允许抛异常**，框架的包装函数（如 `fz_fill_text`）会捕获并 `fz_disable_device` 清空整张虚表，使 device「出错即报废」，再把异常向上 rethrow。
- **容器（clip/group/mask/tile）的 scissor 栈由包装层自动维护**，除非你要像 trace 那样自己跟踪缩进层级，否则不必自管。
- 驱动方式与任何 device 一致：`fz_run_page`（或对显示列表 `fz_run_display_list`），结束 `fz_close_device` + `fz_drop_device`，释放顺序 device → page → doc → ctx。
- 自定义 device 的典型用途：内容统计、调试可观测、自定义矢量导出、指令过滤（passthrough 模式）。

## 7. 下一步学习建议

- **u10-l3 扩展：新增格式与输出 handler** 会把 device 与 document writer 两条扩展线收束在一起——你会发现 document writer 的 `begin_page` 返回的正是 device（u6-l1），本讲写 device 的能力直接服务于「新增输出格式」。
- 想看更「重」的 device 实现，可读 [source/fitz/draw-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/draw-device.c)（光栅化后端，u4-l3）和 [source/fitz/stext-device.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stext-device.c)（结构化文本抽取，u5-l2），它们都遵循本讲的派生 + 填表范式，但回调体复杂得多。
- 若对「在不改格式解释器的前提下拦截指令」感兴趣，阅读 [device.h:402-404](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L402-L404) 的 passthrough device 宏，以及 culling device（[device.h:574-605](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/device.h#L574-L605)）的源码，体会「包一层只 override 部分回调」的写法。
