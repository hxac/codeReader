# u3-l1 GGUF 内存映射加载

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 ds4 为什么用 `mmap` 把几十到几百 GB 的 GGUF 文件「映射进进程」而不是「读进内存」，以及这样做省下了什么。
- 读懂 `ds4.c` 里 `ds4_model` / `ds4_tensor` / `ds4_kv` 这一组结构，以及围绕它们的 `model_open` → `parse_metadata` → `parse_tensors` 加载链。
- 区分 **Metal 共享映射（`MAP_SHARED`）** 与 **CPU 私有映射（`MAP_PRIVATE`）**，并解释为什么 CPU 路径刻意避开共享映射。
- 解释 `inspect_only` 模式跳过了哪些步骤，从而理解 `--inspect` 这个排错入口。
- 理解 `config_validate_model` 如何把 GGUF 里读到的元数据与编译期常量逐字段对齐，实现「这个文件确实是 DeepSeek V4」的校验。

本讲承接 [u2-l1](u2-l1-engine-api-boundary.md) 的「`ds4_engine_open` 消费配置包」结论——`ds4_engine_open` 第一步就是 mmap 加载模型。本讲只盯加载，不展开推理内核（那是 u4 的事）。

## 2. 前置知识

### 2.1 什么是 mmap

`mmap`（memory map）是 POSIX 提供的系统调用，它把一个文件「贴」到进程的虚拟地址空间：

- 你拿到一个指针 `map`，可以像访问普通内存一样 `map[i]` 读取文件的第 `i` 个字节。
- 操作系统**不会**一次性把整个文件读进物理内存（RAM）。它只建立「虚拟地址 → 文件偏移」的映射表，等你真正访问某个字节时，才触发一次 **缺页中断（page fault）**，由内核按页（通常 4 KiB）把对应内容从磁盘读进页缓存（page cache）。
- 因此 mmap 一个 100 GB 的文件，进程的虚拟地址空间会「占用」100 GB，但物理内存占用只取决于你**实际碰过**哪些页。

ds4 的 GGUF 动辄几十 GB（2bit）到几百 GB（4bit / PRO），远超普通机器的可用内存。mmap 让 ds4「假装已经把模型全装进了内存」，实际按推理需要再逐页加载，这是它在个人电脑上跑得起来的关键之一。

### 2.2 MAP_SHARED 与 MAP_PRIVATE

`mmap` 的 `flags` 参数里，对只读映射最常用的两种：

| 标志 | 含义 | 典型用途 |
|------|------|----------|
| `MAP_SHARED` | 映射与文件、以及与其它映射同一文件的进程**共享**物理页；写入（如果有写权限）会回写文件 | 多个进程共享同一文件、或需要把映射「借」给另一个子系统（如 GPU 驱动）时 |
| `MAP_PRIVATE` | 写时复制（copy-on-write）的私有映射；对 ds4 这种只读场景，意味着「我自己用这份只读视图，不与文件共享写语义」 | 普通的只读文件读取 |

ds4 会根据后端在这两者之间切换，原因见 4.2。

### 2.3 GGUF 文件布局（极简版）

GGUF（GPT-Generated Unified Format）是 llama.cpp 系生态的二进制模型容器。它的开头是一个固定头：

```
magic   : u32   "GGUF"（小端 0x46554747）
version : u32   格式版本（ds4 只认 v3）
n_tensors: u64  张量描述符个数
n_kv    : u64   元数据键值对个数
```

头之后是：

1. **元数据表（metadata）**：`n_kv` 个键值对，存模型形状、词表、量化参数等。
2. **张量目录（tensor directory）**：`n_tensors` 个张量描述符，记录每个张量的名字、维度、类型、**相对偏移**。
3. **张量数据区（tensor data）**：真正的权重字节，按 `alignment`（默认 32，可被 `general.alignment` 改写）对齐。

关键点：**张量目录里存的是相对偏移，真正的字节在张量数据区**。加载时只需读元数据表 + 张量目录（它们都很小），就能知道每个张量的字节在文件里的位置，然后用「映射基址 + 偏移」直接访问，不必把权重拷出来。

> 关于 GGUF 的更多细节（量化块结构等）属于 [u3-l4](u3-l4-quantization-formats.md) 的范围，本讲只需上面这点布局知识。

### 2.4 cursor（字节游标）抽象

ds4 解析 GGUF 头/元数据/张量目录时，统一用一个叫 `ds4_cursor` 的「字节游标」顺序读字节。它本质是 `{基地址, 总大小, 当前位置 pos, 错误信息}` 四元组，每读几个字节就把 `pos` 往前推，并在越界时报错。这是一种很常见的「流式解析小工具」，我们在 4.1 会看到它的定义。

## 3. 本讲源码地图

| 文件 | 本讲关心的部分 | 作用 |
|------|----------------|------|
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | `ds4_engine_options` 结构、`inspect_only` 字段 | 声明配置包里哪个字段控制「只看不动」 |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | `ds4_cursor`、`ds4_model` / `ds4_tensor` / `ds4_kv`、`model_open`、`parse_metadata`、`parse_tensors`、`model_close`、`model_prefetch_cpu_mapping`、`config_validate_model`、`ds4_engine_open` 里的调用点 | 本讲全部核心逻辑 |
| [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | `--inspect` 选项解析 | 暴露给用户的「只看模型摘要」入口 |

> 提示：`ds4_model` 是定义在 `ds4.c` 文件内部（`static` 作用域链上的）私有类型，并不出现在 `ds4.h` 里——它属于引擎实现细节，前端只通过不透明的 `ds4_engine *` 句柄间接持有它（见 4.1）。

---

## 4. 核心概念与源码讲解

### 4.1 ds4_model：GGUF 在内存里的「目录」

#### 4.1.1 概念说明

`ds4_model` 不是一个把权重都拷进来的大容器。它更像一张「**目录卡片**」：记录「文件被映射到哪个地址、多大、有几个元数据项、有几个张量、张量数据区从哪开始」，以及两个指针数组——`kv`（元数据表）和 `tensors`（张量目录）。

推理代码访问某个权重时，做的是：

```
权重字节地址 = m->map + tensor->abs_offset
```

也就是「映射基址 + 张量在文件里的绝对偏移」。**权重本身永远待在 mmap 区域里，没有被拷贝到 `ds4_model` 的任何字段中。** 这是「零拷贝加载」的核心。

`ds4_model` 被嵌在 `ds4_engine` 结构体里（按值嵌入，不是指针），所以「打开引擎」就等于「mmap 了模型」：

```c
struct ds4_engine {
    ds4_model model;        // 主模型
    ds4_model mtp_model;    // MTP 投机解码用的 draft 模型（可选）
    ...
};
```

#### 4.1.2 核心流程

`ds4_model` 的生命周期很简单：

```
[空的 ds4_model] --model_open()--> [已映射 + 已解析目录的 ds4_model]
                                         |
                          推理期间只读 m->map + tensor->abs_offset
                                         |
                                    model_close() --> munmap + 释放目录数组
```

`ds4_model` 本身在整个引擎生命周期里基本是**只读**的——映射建立后，它的字段（`map`、`size`、`kv`、`tensors`…）不再改变，所有推理路径都从它「查地址」。这也呼应了 [u2-l1](u2-l1-engine-api-boundary.md) 讲过的「engine 进程级、基本只读」。

#### 4.1.3 源码精读

先看 `ds4_model` 本体，注意它把「映射」和「目录」分得很清楚：

[ds4.c:1616-1630](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1616-L1630) 定义 `ds4_model`：`fd`/`map`/`size` 描述 mmap 区域；`version`/`n_kv`/`n_tensors`/`alignment`/`tensor_data_pos` 是从 GGUF 头读出的元信息；`kv` 和 `tensors` 是两个动态分配的指针数组，存放「目录」。

[ds4.c:1605-1614](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1605-L1614) 定义目录里每一条记录的样子——`ds4_kv`（一个元数据键值对：键字符串、值类型、值在文件里的位置）和 `ds4_tensor`（一个张量描述符：名字、维数、各维大小、类型、相对/绝对偏移、元素数、字节数）。

`ds4_tensor` 里同时有 `rel_offset`（GGUF 文件里写的相对偏移）和 `abs_offset`（加载时算出的、相对于映射基址的绝对偏移），还有 `bytes`（这个张量占多少字节）。三者合起来正好够算出「这个张量的字节在 `m->map` 的哪个区间」。

解析时用到的游标类型：

[ds4.c:611-616](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L611-L616) 定义 `ds4_cursor`：`base`（映射基址）、`size`（文件大小）、`pos`（当前读位置）、`error`（错误缓冲）。

围绕它的一组小函数实现「带边界检查的顺序读」，注意它们都先问 `cursor_has` 够不够字节，够才读并推进 `pos`：

[ds4.c:1481-1523](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1481-L1523) 提供 `cursor_has`（越界检查）/`cursor_read`（读 n 字节到 dst）/`cursor_skip`（跳过 n 字节）/`cursor_u32`/`cursor_u64`/`cursor_string`（先读 u64 长度，再记录字符串指针，**不拷贝字符串内容**），以及把任意位置向上对齐到 `alignment` 的 `align_up`。

一个关键的「构造 cursor」辅助函数，所有「我想从文件某个偏移开始读」的代码都从这里起手：

[ds4.c:1714-1722](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1714-L1722) `cursor_at(m, pos)`：以模型的 `map` 为 `base`、`size` 为总大小、`pos` 为起点造一个游标。后续 `model_get_u32` 等「按键查元数据」的函数都是先用它定位到值的字节位置，再解码（见 4.4）。

> 注意 `cursor_string`（[ds4.c:1510-1518](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1510-L1518)）记录的 `ds4_str.ptr` 直接指向 mmap 区域里的字节，而不是 `malloc` 一份拷贝。所以整个解析阶段——元数据表、张量目录、甚至字符串——**几乎没有大块内存分配**，只有 `kv` 和 `tensors` 两个目录数组被 `calloc` 出来。

#### 4.1.4 代码实践

**实践目标**：亲手验证「权重没有被拷贝，只是被映射」。

**操作步骤**：

1. 打开 `ds4.c`，定位 `ds4_model` 定义（约 1616 行）。
2. 在仓库里全局搜索 `m->map +` 或 `tensor->abs_offset`，观察推理代码（如 `tensor_data`、`matvec_*` 系列）如何取权重地址。
3. 定位 [ds4.c:2326](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2326) 附近的 `tensor_data(m, t)`：它就是「`m->map + t->abs_offset`」一行封装。

**需要观察的现象**：取权重地址的代码统一是「映射基址 + 偏移」，而不是「从某个缓冲区读」。

**预期结果**：你会看到权重字节始终来自 mmap 区域，`ds4_model` 里没有任何「拷贝出来的权重」字段——这印证了零拷贝设计。

#### 4.1.5 小练习与答案

**练习 1**：`ds4_model` 里的 `kv` 和 `tensors` 是「目录」还是「数据」？为什么它们用 `calloc` 分配，而权重不用？

> **参考答案**：是「目录」（描述符）。`kv` 存每个元数据项的键/类型/值位置，`tensors` 存每个张量的名字/维度/偏移/字节数。它们小而需要随机访问，所以分配到堆上。权重字节巨大，按需页入即可，留在 mmap 区域里用 `map + offset` 直接寻址，不必也不能整体 `malloc`。

**练习 2**：`ds4_tensor` 里有 `rel_offset` 和 `abs_offset` 两个字段。它们什么时候被分别填入？

> **参考答案**：`rel_offset` 在 `parse_tensors` 解析张量目录时直接从 GGUF 字节读入（相对张量数据区的偏移）；`abs_offset` 在同一函数末尾用 `tensor_data_pos + rel_offset` 计算得到（相对映射基址的偏移）。推理代码用的是 `abs_offset`。

---

### 4.2 model_open：mmap 一次、零拷贝

#### 4.2.1 概念说明

`model_open` 是「把一个 GGUF 变成可用 `ds4_model`」的总入口。它做四件事：

1. `open` 文件、`fstat` 拿大小、`mmap` 把整个文件映射进来。
2. 读 GGUF 固定头（magic / version / n_tensors / n_kv），做基本合法性检查。
3. `parse_metadata` 解析元数据表。
4. `parse_tensors` 解析张量目录，并把相对偏移换算成绝对偏移。

它的签名很有讲究：

```c
static void model_open(ds4_model *m, const char *path,
                       bool metal_mapping, bool prefetch_cpu);
```

- `metal_mapping`：是否需要「Metal 友好」的映射方式（见下）。
- `prefetch_cpu`：CPU 路径是否要顺带 `madvise(WILLNEED)` 提示内核预热（见 4.3）。

注意返回类型是 `void`，但失败时它会调用 `ds4_die` 直接 `exit(1)`——也就是说，**加载失败即进程退出**，不靠返回值传递错误。这是 ds4 一致的风格（`ds4_die` 在 [ds4.c:618-621](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L618-L621) 打印后退出）。

#### 4.2.2 核心流程

```
model_open(m, path, metal_mapping, prefetch_cpu)
│
├─ memset(m, 0); m->fd = -1;             // 失败安全：先清零
├─ fd = open(path, O_RDONLY)
├─ fstat(fd) → st.st_size                // 拿文件大小，太小直接 die
│
├─ mmap_flags = metal_mapping ? MAP_SHARED : MAP_PRIVATE
├─ map = mmap(NULL, size, PROT_READ, mmap_flags, fd, 0)
├─ m->{fd,map,size} = ...                // 记录映射三件套
│
├─ 游标 c = cursor_at(m, 0)              // 从文件头开始读
├─ 读 magic → 必须等于 DS4_GGUF_MAGIC（"GGUF"）
├─ 读 version → 必须是 3
├─ 读 n_tensors, n_kv
│
├─ parse_metadata(m, &c)                 // 解析元数据表
├─ parse_tensors(m, &c)                  // 解析张量目录 + 算绝对偏移
│
└─ if (!metal_mapping && prefetch_cpu)
       model_prefetch_cpu_mapping(m)     // CPU 路径可选预热
```

为什么 Metal 要 `MAP_SHARED`、CPU 要 `MAP_PRIVATE`？答案是「**让 GPU 直接复用这份映射，避免拷贝上传**」vs「**避开一个 Darwin 内核 bug**」。

- **Metal 路径**：Apple 的 Metal 框架支持把一段**文件 backed 的共享映射**包装成「零拷贝 `MTLBuffer`」（`newBufferWithBytesNoCopy:length:options:`）。这样 GPU 直接从同一份物理页读权重，不必先 `memcpy` 到一块 GPU 私有缓冲再上传。要实现零拷贝，映射必须是 `MAP_SHARED`（共享、文件 backed）。
- **CPU 路径**：CPU 后端只是用普通指针读权重，不需要 Metal 的零拷贝语义。代码注释里写明：曾经观察到在 Darwin 上用共享映射流式读超大 GGUF 时，内核在「VM map-count 计数」路径上发生 **内核恐慌（kernel panic）**，而不是返回一个正常的用户态错误。于是 CPU 路径刻意改用 `MAP_PRIVATE`，绕开这条出过问题的内核路径。

这是一个很好的「**工程取舍**」案例：两条路径的 `mmap` flags 不同，不是因为语义需求不同，而是因为一个具体的内核 bug。

#### 4.2.3 源码精读

[ds4.c:1945-1991](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1945-L1991) 是 `model_open` 全部。重点看三段：

1. 打开 + 映射 + 选 flags（注意那大段注释解释 CPU 为何不用 `MAP_SHARED`）：
   [ds4.c:1969-1971](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1969-L1971) 用三目运算符选 `MAP_SHARED`/`MAP_PRIVATE`，再 `mmap`。
2. 读固定头并校验：
   [ds4.c:1977-1985](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1977-L1985) 顺序读 magic / version / n_tensors / n_kv；magic 不符报「not a GGUF file」，version 非 3 报「only GGUF v3 is supported」。这里 `DS4_GGUF_MAGIC` 定义在 [ds4.c:601](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L601)（`0x46554747u`，即小端 "GGUF"）。
3. 末尾的条件预热：
   [ds4.c:1990](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1990) 仅当「非 Metal」且「请求了 CPU 预热」时调用 `model_prefetch_cpu_mapping`。

接着看两个解析函数。

**元数据表解析**——`parse_metadata`：它为 `n_kv` 个键值对各建一条 `ds4_kv` 记录，**只记下值在文件里的位置 `value_pos`，当时并不解码值**（值留在 mmap 里，谁需要谁再去解）。唯一当场消费的是 `general.alignment`：

[ds4.c:1859-1885](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1859-L1885)。注意 [ds4.c:1863](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1863) 默认 `alignment = 32`，若元数据里写了 `general.alignment` 就覆盖它。这种「先记位置、惰性解码」的设计让加载阶段几乎不解码任何值——直到 `config_validate_model`（4.4）才真正按需解码需要校验的字段。

**张量目录解析**——`parse_tensors`：读每个张量描述符（名字/维数/各维/类型/相对偏移），用 `tensor_nbytes` 算字节数，然后把相对偏移换算成绝对偏移并做越界检查：

[ds4.c:1889-1939](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1889-L1939)。其中 [ds4.c:1922](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1922) 算出张量数据区的起点 `tensor_data_pos = align_up(当前 pos, alignment)`（按元数据里的对齐值对齐）；[ds4.c:1929](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1929) 换算 `abs_offset = tensor_data_pos + rel_offset`；[ds4.c:1930-1934](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1930-L1934) 检查每个张量的字节区间不超出文件末尾（否则 `die("tensor points outside GGUF file")`）。

**释放**——`model_close`：逆序回收资源，对 `NULL` 安全：

[ds4.c:1823-1831](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1823-L1831)：`free(kv)` → `free(tensors)` → `munmap(map, size)` → `close(fd)` → `memset` 清零并把 `fd` 重置为 -1。这一份「`m->fd = -1`」很重要：它保证一个关过的 `ds4_model` 再次被 `model_close` 时不会误 `close` 一个被复用的 fd。

#### 4.2.4 代码实践

**实践目标**：在源码里追踪「`metal_mapping` 这个 bool 是从哪传进来的」，从而理解它和后端的绑定关系。

**操作步骤**：

1. 在 `ds4.c` 搜 `model_open(`，会找到三个调用点（约 24935、25606、25690 行）。
2. 重点看 `ds4_engine_open` 里的调用：[ds4.c:25604-25609](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25604-L25609)。注意 `graph_backend = ds4_backend_uses_graph(opt->backend)`，再 `model_open(&e->model, opt->model_path, graph_backend, !opt->inspect_only)`。
3. 看 `ds4_backend_uses_graph` 的定义：[ds4.c:75-77](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L75-L77)，它对 `DS4_BACKEND_METAL` 和 `DS4_BACKEND_CUDA` 返回 `true`（ROCm 复用 CUDA 这一位，见 [u1-l4](u1-l4-build-and-backends.md)）。

**需要观察的现象**：`metal_mapping` 形参的实参其实是 `graph_backend`——即「后端是不是图后端（Metal/CUDA/ROCm）」。参数名叫 `metal_mapping` 只是历史命名，语义已泛化为「需要 GPU 零拷贝友好映射」。

**预期结果**：你能复述出——Metal/CUDA/ROCm 后端 → `MAP_SHARED`；CPU 后端（或 `DS4_NO_GPU` 构建）→ `MAP_PRIVATE`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CPU 路径不也用 `MAP_SHARED`？理论上只读映射两者都能工作。

> **参考答案**：纯语义上两者都行。但 ds4 的 CPU 路径流式读超大 GGUF 时，在 Darwin 上用共享映射触发过内核在 VM map-count 计数路径上的 panic；改用 `MAP_PRIVATE` 能绕开这条出过问题的内核代码路径，同时不影响 CPU 后端只用普通指针读权重的事实。这是一个针对具体内核 bug 的防御性取舍（见 [ds4.c:1957-1968](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1957-L1968) 的注释）。

**练习 2**：`parse_metadata` 读到一个键值对时，把它的「值」读出来了吗？

> **参考答案**：没有。它只记下 `kv->value_pos = c->pos`（值在文件里的起点）和类型，然后 `skip_value` 跳过值的字节。值真正被解码发生在更后面——比如 `config_validate_model` 用 `model_get_u32` 等按需读取时。这种「记位置、惰性解码」避免了对全部元数据的提前解码。

---

### 4.3 inspect_only：只看不动

#### 4.3.1 概念说明

`inspect_only` 是 `ds4_engine_options` 里的一个布尔字段（[ds4.h:115](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L115)）。它对应命令行 `--inspect`：**只把模型映射进来、读它的元数据、打印一份摘要，然后退出**，不初始化 GPU、不加载词表、不做任何推理。

它的用途是排错与自检：

- 「我下载的这个 GGUF 到底是不是 DeepSeek V4？多少层？多少专家？文件多大？」——`./ds4 --inspect` 一秒回答。
- 验证一份新量化/新下载的模型能否被 ds4 接受（能不能通过 `config_validate_model`）。

在 `model_open` 层面，`inspect_only` 直接影响 `prefetch_cpu` 形参：`ds4_engine_open` 传入 `!opt->inspect_only`，也就是「**inspect 模式下不做 CPU 预热**」。原因是预热（`madvise WILLNEED`）会让内核开始把整个超大张量区往页缓存里拉，而 inspect 只是看元数据，完全不需要碰张量字节——预热反而是浪费。

#### 4.3.2 核心流程

`ds4_engine_open` 里 `inspect_only` 影响的步骤（按代码顺序）：

```
ds4_engine_open(opt)
│
├─ model_open(..., prefetch_cpu = !inspect_only)   // inspect → 不预热
├─ if (!inspect_only) vocab_load(...)              // inspect → 不加载词表
├─ config_validate_model(...)                      // inspect → 仍然校验形状
├─ weights_bind(...)                               // inspect → 仍然绑定权重目录
├─ ...（SSD 缓存预算等计算）...
│
├─ if (inspect_only) { *out = e; return 0; }       // ★ inspect 在这里提前返回
│
└─ （以下 inspect 全部跳过）
   ├─ CPU 方向引导加载
   ├─ MTP draft 模型加载
   └─ ds4_gpu_init() 等 GPU 后端初始化
```

所以 `inspect_only` **跳过**：CPU 映射预热、词表加载、CPU 方向引导、MTP 模型、GPU 后端初始化。
它**保留**：模型 mmap、元数据/张量目录解析、形状校验（`config_validate_model`）、权重目录绑定（`weights_bind`）。

#### 4.3.3 源码精读

调用点与预热开关：

[ds4.c:25606-25609](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25606-L25609)：第四个实参 `!opt->inspect_only` 即 `prefetch_cpu`；紧接着 `if (!opt->inspect_only) vocab_load(...)`——词表加载也只在非 inspect 模式做。注意 `config_validate_model` 在 inspect 模式下**照常执行**，这正是 `--inspect` 能用来验证「这份 GGUF 合不合法」的原因。

CPU 预热本身（被 inspect 跳过的那个函数）：

[ds4.c:1833-1855](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1833-L1855) `model_prefetch_cpu_mapping` 用 `posix_madvise(..., POSIX_MADV_WILLNEED)` 给内核一个提示：这块映射「之后会用到」，可以开始往页缓存里拉了。注释强调它**不拷贝、不 pin** GGUF，只是预热。注意它只在 `!metal_mapping && prefetch_cpu` 时被调用——Metal 路径有自己的 GPU 端预热，不需要它。

提前返回点：

[ds4.c:25673-25676](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25673-L25676)：`if (opt->inspect_only) { *out = e; return 0; }`。这是 inspect 模式的「出口」——返回一个合法的 `ds4_engine *`，但这个引擎还没初始化 GPU。后面所有「真正要用 GPU/CPU 推理」的步骤（CPU 方向引导、MTP、`ds4_gpu_init()` 等）都在它之后，inspect 一概跳过。

例如 GPU 后端初始化就在这个返回点之后：

[ds4.c:25715-25716](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25715-L25716) `if (graph_backend) { e->metal_ready = ds4_gpu_init() != 0; ... }`。inspect 模式不会走到这里，所以即便机器上没有可用的 GPU，`--inspect` 也能正常工作——它根本不碰 GPU。

最后是用户入口与摘要打印：

[ds4_cli.c:1599-1600](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1599-L1600) 解析 `--inspect` 到 `c.inspect`；[ds4_cli.c:1652](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1652) 把它复制进 `cfg.engine.inspect_only`；[ds4_cli.c:1687-1688](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1687-L1688) 在 `inspect` 为真时调用 `ds4_engine_summary(engine)`。

[ds4.c:25977-25979](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25977-L25979) `ds4_engine_summary` 只是委托给内部的 `model_summary(&e->model)`，后者（[ds4.c:1998-2063](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1998-L2063)）用一系列 `model_get_*` 按需解码元数据，打印模型名、架构、层数、上下文长度、注意力头数、专家数、文件大小、张量总字节、逻辑参数量等。

#### 4.3.4 代码实践

**实践目标**：用 `--inspect` 实际观察一次「只加载不推理」，并对照源码理解它跳过了什么。

**操作步骤**：

1. 确保已有一份 GGUF（参考 [u1-l5](u1-l5-download-and-first-run.md) 的下载流程；若没有模型，跳到步骤 3 的源码阅读部分）。
2. 运行：
   ```bash
   ./ds4 --inspect
   ```
   （`ds4`/`ds4-server` 默认读 `ds4flash.gguf` 软链，见 [u1-l5](u1-l5-download-and-first-run.md)。）
3. 阅读打印的摘要，对照 `model_summary`（[ds4.c:1998-2063](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1998-L2063)）确认每一行来自哪个元数据键。

**需要观察的现象**：程序几乎瞬间打印摘要后退出，不会触发任何 GPU 初始化日志、不会加载词表、CPU 占用也只来自读元数据那点字节。

**预期结果**：你看到类似「model / arch / gguf v3 / layers / attention / experts / file size / tensor bytes / logical parameters」的输出。如果模型与 ds4 期望的形状不符，会在打印摘要**之前**就因 `config_validate_model` 失败而退出（见 4.4）。

> 若无法本地运行（没有模型或不在支持的平台上），请改做源码阅读型实践：在 `ds4_engine_open` 里数清楚 `inspect_only` 为真时，「`model_open` 之前」和「提前返回点之后」分别有哪些步骤被跳过。

#### 4.3.5 小练习与答案

**练习 1**：`--inspect` 模式下，`config_validate_model` 会执行吗？为什么这样设计？

> **参考答案**：会执行（见 [ds4.c:25609](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25609) 在 inspect 提前返回之前）。这样设计让 `--inspect` 既能打印摘要，又能顺便验证「这份 GGUF 的形状和 ds4 编译期常量是否一致」——这正是 inspect 作为排错入口的价值。

**练习 2**：为什么 inspect 模式要让 `prefetch_cpu = false`？

> **参考答案**：`madvise(WILLNEED)` 会让内核开始把巨大的张量数据区预读进页缓存，而 inspect 只需要读元数据/张量目录（文件开头一小段），完全不需要张量字节。预热会白白占用内存和 IO，所以 inspect 把它关掉。这也说明 `prefetch_cpu` 是一个与 `metal_mapping` 正交的、独立的「是否预热 CPU 映射」开关。

---

### 4.4 config_validate_model：元数据与编译期形状对齐

#### 4.4.1 概念说明

ds4 是一个**专用**引擎（见 [u1-l1](u1-l1-project-overview.md)）：它不是通用 GGUF runner，而是为 DeepSeek V4 的精确形状写死的。所以加载模型时，必须确认「这个 GGUF 描述的模型，和我编译进二进制的期望形状一模一样」。

这件事由 `config_validate_model` 完成。它做的事可以概括为：

1. 用一系列 `required_*` 辅助函数从元数据里**取出**关键字段（层数、嵌入维、词表大小、注意力头、专家数、indexer 维度、hyper-connection 参数…）——取不到就 `die`（说明 GGUF 缺关键字段）。
2. 把取出的值喂给 `ds4_select_shape_from_metadata`，再用一大批 `config_expect_u32("字段名", 实际值, 期望常量)` 逐字段比对——**实际值 ≠ 编译期常量就 `die`**。
3. 额外校验几个浮点参数（RoPE freq base、RMS eps 等）和 `rope.scaling.original_context_length`。

这种「GGUF 元数据 vs 编译期 `DS4_*` 常量」逐字段对齐，是 ds4「官方向量校验」哲学在加载阶段的体现：宁可早死，也不带着形状不匹配的模型继续跑出错误结果。

#### 4.4.2 核心流程

```
config_validate_model(m)
│
├─ required_u32/f32/bool(m, "deepseek4.xxx")   // 取字段，缺失即 die
│   例：n_layer, n_embd, n_vocab, n_head, n_expert ...
│
├─ ds4_select_shape_from_metadata(取出的字段...)  // 由元数据「选定」编译期形状
│
├─ config_expect_u32("字段名", 实际值, DS4_期望)  // 逐字段比对，不等即 die
│   例：embedding_length==DS4_N_EMBD, expert_count==DS4_N_EXPERT ...
│
├─ config_validate_fixed_shape(n_layer)          // 固定层结构
├─ validate_compress_ratio_metadata(m)           // 压缩比布局
├─ validate_swiglu_clamp_metadata(m)             // SwiGLU clamp
│
└─ 校验 RoPE / compress_rope / expert_weights_scale / rms_eps 等浮点参数
```

其中「取字段」依赖一组小工具：`required_u32`（[ds4.c:3086](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3086) 附近）内部就是「`model_get_u32` 取不到就 `die`」。

#### 4.4.3 源码精读

[ds4.c:3888-3998](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3888-L3998) 是 `config_validate_model` 全部。注意它的几个层次：

- **取字段**：[ds4.c:3889-3914](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3889-L3914) 一连串 `required_u32` 把 DeepSeek V4 所有形状参数从元数据里抠出来。这些字段名（`deepseek4.block_count`、`deepseek4.expert_count`、`deepseek4.attention.indexer.top_k` 等）正是 ds4 专用 GGUF 写入的元数据键。
- **选定 + 比对**：[ds4.c:3916-3937](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3916-L3937) 调 `ds4_select_shape_from_metadata`；[ds4.c:3939-3962](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3939-L3962) 一排 `config_expect_u32("xxx", 实际, DS4_常量)`，比如 `expert_count` 必须等于 `DS4_N_EXPERT`、`attention.key_length` 必须等于 `DS4_N_HEAD_DIM`。
- **浮点与开关**：[ds4.c:3977-3997](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3977-L3997) 校验 RoPE freq base、scaling factor、RMS eps、expert weights scale/norm 等浮点和布尔参数。

「按需解码」靠的是 `model_get_u32` 这一族函数：它们先用 `model_find_kv` 在 `kv` 目录里按名查找（线性扫描），找到就用 `cursor_at(m, value_pos)` 定位到值的字节再解码：

[ds4.c:1724-1743](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1724-L1743)：`model_find_kv` 线性查键；`model_get_u32` 用 `cursor_at(m, kv->value_pos)` 起一个游标并 `cursor_u32` 解码。这正是 4.1 里说的「`parse_metadata` 只记位置，值留给后来人按需解码」的「后来人」。

> 这也解释了为什么 `--inspect` 能又快又全地报告模型形状：所有信息都来自元数据表，而元数据表在 `parse_metadata` 阶段就已经建好了索引（目录），`model_get_*` 只是查表 + 解码几个字节。

#### 4.4.4 代码实践

**实践目标**：读懂「字段名 → 编译期常量」的对应关系，理解 ds4 为何对模型形状零容忍。

**操作步骤**：

1. 打开 [ds4.c:3888-3998](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3888-L3998)。
2. 选两三行 `config_expect_u32(...)`，记下「GGUF 字段名」和「期望常量名」（如 `expert_count` → `DS4_N_EXPERT`）。
3. 在 `ds4.c` / 头文件里搜这些 `DS4_*` 常量的定义，看它们是写死的数字。

**需要观察的现象**：期望值是编译期常量（如某固定层数、固定专家数），而不是「从模型读什么就用什么」。

**预期结果**：你能解释——如果有人拿来一个层数或专家数不同的 GGUF，即便它能被 `parse_metadata`/`parse_tensors` 正确解析，也会在 `config_expect_u32` 这一步因为「实际 ≠ 期望」而 `die`，从而阻止形状不匹配的模型进入推理。

#### 4.4.5 小练习与答案

**练习 1**：`config_validate_model` 为什么用 `required_u32` 而不是 `model_get_u32`？两者区别是什么？

> **参考答案**：`model_get_u32` 取不到字段时返回 `false`（不报错）；`required_u32` 在取不到时调用 `ds4_die` 直接退出。校验阶段要求这些字段**必须存在**（缺了说明 GGUF 不是 DeepSeek V4），所以用「取不到就死」的 `required_*` 版本。

**练习 2**：`config_validate_model` 里有一行校验 `expert_group_count` 期望值是 `0`（[ds4.c:3954](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3954) 附近 `config_expect_u32("expert_group_count", n_expert_groups, 0)`）。这说明了什么？

> **参考答案**：说明 DeepSeek V4 的 MoE **不使用分组路由**（`expert_group_count` 应为 0）。ds4 把「这个字段必须是 0」也写进校验，确保加载的是不带分组路由的 V4 变体。这是「专用引擎」对模型形状的细粒度契约。

---

## 5. 综合实践

**任务**：给一个「假装加载模型」的最小调用链画一张时序图，并把每一步映射到本讲讲过的源码位置。

假设用户执行 `./ds4 --inspect`，请按顺序写出从命令行到打印摘要经过的关键函数与文件位置，并标注「inspect 模式跳过了哪几步」。

参考作答框架（请你补全每一步对应的源码行号/永久链接）：

1. `main`（ds4_cli.c）解析 `--inspect` → `c.inspect = true`。
2. `cfg.engine.inspect_only = cfg.inspect`，调用 `ds4_engine_open`。
3. `ds4_engine_open` 计算出 `graph_backend`，调用 `model_open(..., graph_backend, !inspect_only)`。
4. `model_open` 内部：`open`/`fstat`/`mmap`（flags 取决于后端）→ 读 GGUF 头校验 magic/version → `parse_metadata` → `parse_tensors` → （inspect 下不预热）。
5. 回到 `ds4_engine_open`：跳过 `vocab_load`，执行 `config_validate_model`、`weights_bind`，命中 `if (inspect_only) return`。
6. `ds4_engine_summary` → `model_summary` 打印摘要。

**需要观察的现象**：你能指出「inspect 跳过的三件大事」——词表加载、CPU 映射预热、GPU 后端初始化——分别在源码的哪一行被 `if (!inspect_only)` 或「提前返回点」挡掉。

**预期结果**：一张清晰的时序图 + 一份「inspect 跳过清单」，每条都带可点击的永久链接。这是后续阅读 u3-l2（权重绑定）、u3-l3（词表）时的导航基础。

> 若你想更进一步：在 `model_open` 末尾的 `if (!metal_mapping && prefetch_cpu) model_prefetch_cpu_mapping(m);`（[ds4.c:1990](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1990)）临时加一行 `fprintf(stderr, "prefetch=%d metal=%d\n", prefetch_cpu, metal_mapping);`（**示例代码，仅用于学习，勿提交**），分别用 `--inspect`、正常推理、`--cpu` 跑一次，观察三者打印的差异——这能直观印证 4.2 和 4.3 的结论。改完记得还原，本讲不改源码。

## 6. 本讲小结

- `ds4_model` 是 GGUF 的「**目录卡片**」：它持有 mmap 三件套（`fd`/`map`/`size`）和两个目录数组（`kv`/`tensors`），**权重字节始终留在 mmap 区域**，推理代码用 `map + tensor->abs_offset` 直接寻址，全程零拷贝。
- `model_open` 一次性 mmap 整个文件，读 GGUF 固定头（magic/version/n_tensors/n_kv）校验后，用 `parse_metadata`（只记值位置、不解码）和 `parse_tensors`（把相对偏移换算成绝对偏移并做越界检查）建好目录。
- 后端决定 `mmap` flags：图后端（Metal/CUDA/ROCm）用 `MAP_SHARED` 以支持 GPU 零拷贝缓冲；CPU 后端用 `MAP_PRIVATE`，刻意避开一个 Darwin 内核在共享映射上的 panic bug。
- `inspect_only`（`--inspect`）是「只看不动」入口：跳过 CPU 映射预热、词表加载、GPU 后端初始化等步骤，在 `config_validate_model` + `weights_bind` 之后提前返回，专门用来打印模型摘要和验证 GGUF 合法性。
- `config_validate_model` 把 GGUF 元数据里读到的形状参数与编译期 `DS4_*` 常量逐字段对齐，是 ds4「专用引擎」在加载阶段的形状契约——形状不匹配直接 `die`。
- 解析全程用 `ds4_cursor` 游标做带边界检查的顺序读，字符串/元数据值都不拷贝，只记录指针与位置，保证加载阶段内存开销极小。

## 7. 下一步学习建议

- **接下来读 [u3-l2 权重绑定与张量布局](u3-l2-weights-binding.md)**：本讲看到 `weights_bind` 在 `ds4_engine_open` 里被调用（inspect 也跑），下一讲就拆开它——看 `weights_bind_layer` 如何把 `ds4_tensor` 描述符按层绑定成 `ds4_layer_weights`，以及 MoE expert 在张量目录里如何排布。
- **想理解词表与聊天模板**：去看 [u3-l3 分词器与聊天模板渲染](u3-l3-tokenizer-and-chat-template.md)，它讲解被 inspect 模式跳过的 `vocab_load`。
- **想理解量化块结构**：本讲只提到 `tensor_nbytes` 用 `gguf_type_info` 算字节数；量化块（q2_K/iq2_xxs 等）的内部结构在 [u3-l4 量化格式与张量族](u3-l4-quantization-formats.md)。
- **回到全局**：本讲的 `model_open` 是 `ds4_engine_open` 的第一步；若想重温引擎整体生命周期，回到 [u2-l1](u2-l1-engine-api-boundary.md)。
