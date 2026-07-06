# 目录结构与源码地图

## 1. 本讲目标

读完前两讲，你已经知道 ds4（DwarfStar）是「为 DeepSeek V4 量身打造的自包含原生推理引擎」，也知道了它跑什么模型、用什么量化。但从这一讲开始，我们要真正进入 C 源码。

进入一个约 11 万行的 C 代码库，最怕的不是某一行看不懂，而是「不知道这一行该去哪个文件看」。本讲的目标就是给你一张**可导航的源码地图**：

1. 看到任何一个核心文件名（`ds4.c`、`ds4_server.c`、`ds4_metal.m`……），你能立刻说出它负责什么、不负责什么。
2. 看到 `metal/`、`rocm/`、`gguf-tools/`、`tests/` 这些子目录，你能知道里面放的是哪一类东西。
3. 看到 `README.md`、`AGENT.md`、`Makefile` 这些非源码文件，你能知道它们各自扮演什么「向导」角色。
4. 你能理解一个关键工程取舍：**ds4 用一套共享的「引擎核心」对象，搭配不同的「前端」二进制和不同的 GPU 后端对象**。这套结构是后面所有讲义的基础。

本讲只做「地图」不做「深潜」：我们读的是文件头注释、目录列表和构建脚本，不展开任何算法。等地图建好，后续每一讲才会钻进具体函数。

## 2. 前置知识

本讲假定你已经读过 `u1-l1`（项目定位与设计哲学）和 `u1-l2`（模型与量化策略）。在此之上，只需几个通俗概念：

- **源码文件 vs 头文件**：在 C 项目里，`.c` / `.m` / `.cu` 是「实现」，`.h` 是「声明与约定」。多个 `.c` 要共用同一套数据结构时，会把结构体和函数签名写进一个公共 `.h`，大家各自 `#include` 它。
- **编译单元 / 目标文件（.o）**：每个 `.c` 单独编译成一个 `.o`，最后由链接器拼成可执行文件。`Makefile` 就是描述「哪个 `.o` 依赖哪个源文件、最后拼成哪个二进制」的脚本。
- **后端（backend）**：ds4 可以跑在 Apple Metal、NVIDIA CUDA、AMD ROCm 或纯 CPU 上。「后端」就是「真正干计算活的那一层」。ds4 的一大设计是：**不同的后端用不同的源文件实现，但对外暴露同一套 GPU 接口**，所以上层引擎代码几乎不用改。
- **radix tree（基数树）**：一种按字符串前缀组织的高效查找结构。ds4 用了 antirez 自己的 `rax` 库（没错，就是 Redis 作者的 radix tree）。你只需知道它是个「按键快速查值」的数据结构，细节以后再讲。
- **mmap**：把磁盘上的文件「映射」进内存地址空间，读内存等于读文件，而不必一次性把整个文件读进内存。ds4 加载几十上百 GB 的模型权重时靠的就是它。

> 术语提示：本讲出现「引擎核心（CORE_OBJS）」「前端二进制」「GPU 后端对象」这三个词时，请回看上面的解释——它们是理解目录结构的钥匙。

## 3. 本讲源码地图

本讲重点研读三个「向导型」文件，它们本身就描述了项目结构：

| 文件 | 作用 | 本讲怎么用它 |
| --- | --- | --- |
| `README.md` | 项目主文档，讲怎么下载、构建、运行、调优 | 从中提取「后端清单」「各二进制用途」「子文档索引」 |
| `AGENT.md` | 写给 AI 协作者（也包括你）的工程约束与目录速览 | 它有一节 `Layout` 直接列了核心文件分工 |
| `Makefile` | 构建脚本，定义平台分支与每个二进制由哪些 `.o` 拼成 | 用它反推「引擎核心 + 前端 + 后端」的三层结构 |

此外我们会**列出**（但暂不深入）所有核心源文件与子目录，给你一张完整的导航表。

## 4. 核心概念与源码讲解

按大纲，本讲拆成三个最小模块：**核心源文件分工**、**子目录用途**、**辅助文档与脚本**。

---

### 4.1 核心源文件分工

#### 4.1.1 概念说明

ds4 的根目录平铺着大量 `.c` / `.m` / `.cu` / `.h` 文件，没有 `src/` 子目录包裹——这是 antirez 的典型风格（Redis 也是这样）。这些文件大致分成三类：

1. **引擎核心**：实现「加载模型 → 前向推理 → 采样」这条主链路，与具体怎么对外暴露（CLI 还是 HTTP）无关。代表是 `ds4.c`（以及它的公共头 `ds4.h`）。
2. **GPU 后端**：把引擎核心的计算「落到具体硬件」。代表是 `ds4_metal.m`（Metal）、`ds4_cuda.cu`（CUDA）、`ds4_rocm.cu`（ROCm），它们都实现同一份 `ds4_gpu.h` 接口。
3. **前端二进制**：决定用户怎么用引擎——命令行、HTTP 服务器、agent、评测、基准。每个前端对应一个 `.c` 文件和一个最终二进制。

这三类不是我们凭空分的，而是 `Makefile` 真实的链接关系：每个二进制 = 「自己的前端 `.o` + 若干公共辅助 `.o` + 一组**共享的引擎核心对象 `CORE_OBJS`**」。

#### 4.1.2 核心流程

从「源码文件」到「可执行文件」的拼装流程，可以画成下面这张图（文字版）：

```
                  ds4.h (公共引擎边界：engine / session)
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
       引擎核心            GPU 后端            辅助库
   ds4.c (核心)        ds4_metal.m         rax.c (基数树)
   ds4_distributed.c   ds4_cuda.cu         linenoise.c (行编辑)
   ds4_ssd.c           ds4_rocm.cu         ds4_kvstore.c (磁盘KV)
            │                 │                 │
            └──────────┬──────┴─────────────────┘
                       ▼
              CORE_OBJS = ds4.o + ds4_distributed.o + ds4_ssd.o + <一个后端.o>
                       │
      ┌────────┬───────┼────────┬────────┐
      ▼        ▼       ▼        ▼        ▼
   ds4_cli  ds4_server ds4_bench ds4_eval ds4_agent   ← 各自的前端 .o
      │        │       │        │        │
      ▼        ▼       ▼        ▼        ▼
     ds4   ds4-server ds4-bench ds4-eval ds4-agent    ← 五个二进制
```

要点：

- **同一个引擎核心**（`CORE_OBJS`）被五个二进制复用。所以你改 `ds4.c` 里的推理逻辑，五个程序都会受影响——这正是 `AGENT.md` 反复强调「改一处要测四条路径（Metal / SSD / 分布式 / CUDA）」的原因。
- **后端是可替换的一块**：Metal 构建里 `CORE_OBJS` 含 `ds4_metal.o`；CUDA 构建里换成 `ds4_cuda.o`；ROCm 构建里换成 `ds4_rocm.o`。引擎核心不关心是哪块。
- **CPU 是特例**：CPU 路径不是单独的后端 `.o`，而是把 `ds4.c` 用 `-DDS4_NO_GPU` 重新编译成 `ds4_cpu.o`，编译时直接关掉 GPU 代码、只保留 CPU 参考实现。

#### 4.1.3 源码精读

先看 `AGENT.md` 自己给出的目录速览（这一段就是写给「要在源码里找路」的人看的）：

[AGENT.md:31-43](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L31-L43) — `## Layout` 一节，逐行列出 `ds4.c` / `ds4_cli.c` / `ds4_server.c` / `ds4_metal.m` / `metal/*.metal` / `tests/` / `misc/` 各自负责什么。它最后一句「This list is not complete, check the files for more info」提醒你这只是入口清单。

再来看公共引擎边界。`ds4.h` 顶部有一段「定位说明」注释，定义了整个项目的 API 哲学：

[ds4.h:11-17](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L11-L17) — 把 `ds4_engine` 当作「已加载的模型」，把 `ds4_session` 当作「一条可变的推理时间线」。它强调「保持这个头文件窄，让 HTTP/CLI 代码不依赖张量内部细节」。这是后面 `u2-l1` 的伏笔。

几个前端二进制的「自述注释」同样能帮你快速定位职责：

- [ds4_server.c:7-14](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7-L14) — HTTP 服务器：每个客户端连接一个线程解析请求，再把任务排队给**唯一的 Metal worker 线程**；worker 拥有 `ds4_session`，也就拥有所有活的 KV 缓存状态。
- [ds4_cli.c:6-12](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L6-L12) — 命令行：`-p` 一次性模式拼一个提示就退出；交互模式保留渲染好的 token 转录和一条 session，让多轮对话复用 KV。
- [ds4_distributed.c:1-14](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L1-L14) — 分布式运行时：对外仍然是一个普通的 `ds4_session`，开启分布式时 `ds4.c` 把 sync/eval/save/load 委派给本文件的 coordinator session API。**这个「对上层透明」的设计**是分布式能复用 CLI/server/agent 的关键。
- [ds4_metal.m:25-34](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L25-L34) — Metal 胶水层：C 代码负责模型语义和图调度，这个 Objective-C 文件**只**负责 Metal 对象（设备/队列/库、mmap 权重视图、命令批处理、常驻张量、scratch 缓冲），以及 `metal/` 目录内核的薄封装。

GPU 后端的「统一接口」落在 `ds4_gpu.h`：

[ds4_gpu.h:11-20](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L11-L20) — 说明 GPU API 是 **tensor-resident（张量常驻设备）** 的：激活值、KV 状态、scratch 缓冲在整个 prefill/decode 命令序列里都留在设备上，不来回拷贝。三个后端（`ds4_metal.m` / `ds4_cuda.cu` / `ds4_rocm.cu`）实现的就是这同一组 `ds4_gpu_tensor_*` 与命令原语。

最后，构建脚本把上面这一切「钉死」成确定的关系。`Makefile` 用平台分支定义了 `CORE_OBJS`：

[Makefile:18-41](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L18-L41) — macOS（Darwin）分支里 `CORE_OBJS = ds4.o ds4_distributed.o ds4_ssd.o ds4_metal.o`（第 20 行），Linux 分支换成 `... ds4_cuda.o`（第 31 行）；CPU 构建（第 22、32 行）的 `CPU_CORE_OBJS` 用 `ds4_cpu.o` 且**不含任何后端对象**。这一段就是「引擎核心 + 后端可替换」结构的铁证。

#### 4.1.4 代码实践

**实践目标**：亲手核对这些文件的真实行数与职责，做出一张属于你自己的源码地图表（这也是本讲的正式实践任务，见第 5 节综合实践的简化版）。

**操作步骤**：

1. 在仓库根目录执行下面这条只读命令，得到核心源文件的行数清单：
   ```sh
   wc -l ds4.c ds4.h ds4_server.c ds4_agent.c ds4_distributed.c \
          ds4_metal.m ds4_cuda.cu ds4_rocm.cu ds4_ssd.c \
          ds4_kvstore.c ds4_eval.c ds4_bench.c ds4_cli.c \
          ds4_help.c ds4_web.c ds4_gpu.h rax.c
   ```
2. 对每个文件，用 `head -n 30 <文件>` 读它的顶部注释（C 文件作者习惯在开头写一段「我是谁」）。
3. 把结果填进一张三列表：**文件名 / 行数量级 / 一句话职责**。

**需要观察的现象**：

- `ds4.c`、`ds4_metal.m` 都是 **2.7 万行量级**，是项目里最大的两个文件——前者是引擎核心，后者是 Metal 后端。这一对比能帮你记住「引擎」与「后端」是两笔不同的大代码块。
- `ds4_rocm.cu` 只有约 **130 行**，却支撑整个 ROCm 后端——因为它的实现藏在 `rocm/*.cuh` 头文件里（见 4.2）。这是一个**反直觉**的发现：行数小不代表功能少。
- `ds4_ssd.c` 也只有约 **180 行**——SSD 流式是 README 里的大特性，但这个文件只装「缓存预算/解析/mlock」这类小帮手，真正的按需读取主路径其实在 `ds4.c` 里。
- `rax.c` 顶部带 BSD 版权，写明作者是 Salvatore Sanfilippo——这是从 Redis 项目借用的 radix tree 实现。

**预期结果**：你会得到一张类似下面这样的表（行数为编写本讲义时的实测值，你本地可能因版本略有差异）：

| 文件 | 行数（量级） | 一句话职责 |
| --- | --- | --- |
| `ds4.c` | 27791 | 引擎核心：模型加载、分词、CPU 参考实现、Metal 图调度、session、磁盘 payload 序列化 |
| `ds4.h` | 335 | 公共引擎边界（`ds4_engine` / `ds4_session`） |
| `ds4_server.c` | 15875 | OpenAI/Anthropic 兼容的 HTTP 服务器 |
| `ds4_agent.c` | 10244 | 原生编码 agent（推理在进程内、KV 即会话） |
| `ds4_distributed.c` | 8414 | 分布式推理传输与编排（coordinator/worker） |
| `ds4_metal.m` | 26819 | Metal 运行时与内核封装（Objective-C） |
| `ds4_cuda.cu` | 13256 | CUDA 后端（内核直接写在本文件内） |
| `ds4_rocm.cu` | 131 | ROCm/HIP 后端薄壳，include 了 `rocm/*.cuh` |
| `ds4_ssd.c` | 181 | SSD 流式的预算/解析/mlock 小帮手 |
| `ds4_kvstore.c` | 1359 | 磁盘 KV 缓存文件读写与淘汰策略 |
| `ds4_eval.c` | 4289 | `ds4-eval` 能力评测（92 题集） |
| `ds4_bench.c` | 683 | `ds4-bench` 吞吐基准 |
| `ds4_cli.c` | 1707 | `ds4` 命令行 + linenoise 交互式 REPL |
| `ds4_help.c` | 559 | 各二进制共用的 `--help` 文本 |
| `ds4_web.c` | 1385 | agent 用的 web 搜索（google_search / visit_page） |
| `ds4_gpu.h` | 1024 | 三后端共享的 GPU 张量/命令抽象 |
| `rax.c` | 2747 | radix tree（借用自 Redis，用于精确 DSML 回放等） |

> 待本地验证：行数会随 git 版本变化，以你本机 `wc -l` 输出为准；上表数字是 HEAD `80ebbc3` 处的实测值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ds4_server.c`、`ds4_agent.c`、`ds4_cli.c` 都 `#include "ds4.h"`，但彼此几乎不互相 include？

**参考答案**：因为它们是三个**独立的前端**，共享的是「引擎核心」这同一份后端能力（通过 `CORE_OBJS` 链接进来），而不是彼此的代码。`ds4.h` 是它们共同的窄边界，所以只需 include 它。这也呼应了 `ds4.h:11-17` 「保持头文件窄、前端不依赖张量内部」的设计。

**练习 2**：`ds4_rocm.cu` 只有约 130 行，却实现了整个 ROCm 后端。它的实际计算代码藏在哪里？

**参考答案**：藏在 `rocm/` 目录下的 22 个 `.cuh` 头文件里。`ds4_rocm.cu` 顶部通过 `#include "rocm/ds4_rocm_*.cuh"` 把注意力/前缀、MoE、KV、matmul 等内核全部拉进来（参见 [ds4_rocm.cu:92-131](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_rocm.cu#L92-L131)）。相比之下，CUDA 后端把内核直接写在 `ds4_cuda.cu` 一个 1.3 万行的大文件里——这是两个后端在「代码组织方式」上的显著差异。

**练习 3**：CPU 构建里，`CPU_CORE_OBJS` 为什么**没有** `ds4_metal.o` / `ds4_cuda.o` / `ds4_rocm.o` 中的任何一个？

**参考答案**：因为 ds4 的 CPU 参考实现就**内嵌在 `ds4.c` 里**，用 `-DDS4_NO_GPU` 宏在编译时关掉 GPU 代码路径（见 [Makefile:190-191](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L190-L191) 把 `ds4.c` 编成 `ds4_cpu.o`）。所以 CPU 模式不需要单独的后端对象——引擎核心自身就退化成了 CPU 后端。这与 Metal/CUDA/ROCm「后端是独立的可替换 `.o`」形成对比。

---

### 4.2 子目录用途

#### 4.2.1 概念说明

根目录的平铺 `.c` 文件是「主干」，而几个子目录则是「分类仓库」：把**同类型**的文件归拢到一起。ds4 的子目录都很有规律——通常一种文件后缀对应一个目录：

- `metal/` 放 `.metal` Metal Shading Language 内核；
- `rocm/` 放 `.cuh` HIP/ROCm 内核头；
- `gguf-tools/` 放模型构建期的离线工具（生成 GGUF、收 imatrix、量化、质量打分）；
- `tests/` 放测试；
- `speed-bench/` 放基准数据与绘图脚本；
- `dir-steering/` 放方向性引导的向量与脚本；
- `misc/` 放「被忽略的笔记、实验、旧规划材料」（`AGENT.md` 原话），不是正式文档。

> 重要区分：`gguf-tools/` 里的工具是**离线、模型构建期**用的（在推理开始之前），而根目录的 `ds4*` 程序是**运行期**用的。把这两类分开看，目录结构就清晰了一大半。

#### 4.2.2 核心流程

按「是否参与运行期推理」给子目录分类：

```
运行期会被引擎加载/链接            离线工具与资料（不进运行期）
────────────────────────         ────────────────────────────
metal/   Metal 内核(19个)         gguf-tools/   生成/量化/imatrix/打分
rocm/    ROCm 内核头(22个)        tests/         C 测试 + 测试向量
                                  speed-bench/   基准 CSV + 绘图
                                  dir-steering/  引导向量 + 实验
                                  misc/          旧笔记(忽略)
```

`metal/` 与 `rocm/` 的特殊性：它们**不是**被编译进 `.o` 的普通源码，而是 GPU **内核源**。Metal 内核在运行时由 `ds4_metal.m` 编译加载（macOS 上 `--chdir` 选项就是为了让 `metal/*.metal` 能从项目树被找到）；ROCm 内核则是被 `ds4_rocm.cu` 在编译期 `#include` 进去的 `.cuh` 头。

#### 4.2.3 源码精读

`metal/` 目录下一共有 19 个 `.metal` 文件，命名直接暴露了它们各自的计算职责。挑几个关键的：

- [metal/flash_attn.metal](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/metal/flash_attn.metal) — Flash Attention 内核，注意力计算的核心。
- [metal/dsv4_kv.metal](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/metal/dsv4_kv.metal) — DeepSeek V4 专属的 KV 缓存读写内核（对应 `u4-l2` 要讲的压缩 KV）。
- [metal/moe.metal](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/metal/moe.metal) — MoE（专家混合）路由与专家计算内核。

> 这些 `.metal` 文件被 [Makefile:15](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L15) 用 `$(wildcard metal/*.metal)` 收集成 `METAL_SRCS`，并列为 `ds4_metal.o` 的依赖（[Makefile:208](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L208)）——意思是「任何一个 metal 内核改了，Metal 后端都要重编」。

`rocm/` 目录有 22 个 `.cuh`，命名规则是 `ds4_rocm_<模块>.cuh`，比如 attention、compressor、indexer、moe、matmul、router、shared_expert 等——正好对应 DeepSeek V4 的各个计算子模块。它们被 `ds4_rocm.cu` 逐个 include：

[ds4_rocm.cu:92-131](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_rocm.cu#L92-L131) — 一长串 `#include "rocm/ds4_rocm_*.cuh"`，把运行时、公共工具、q8、norm/rope、fp8 KV、attention、HC、output、indexer、matmul、compressor、shared expert、router、moe 等模块全部拼装进 ROCm 后端。这解释了为什么 `ds4_rocm.cu` 本体只有 130 行却功能完整。

`gguf-tools/` 是一个相对独立的子项目，甚至有自己的 [gguf-tools/Makefile](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/Makefile) 和 [gguf-tools/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md)。它包含：

- `deepseek4-quantize.c` — 从 HuggingFace safetensors + 模板 GGUF 重新生成 Q2/Q4 GGUF；
- `quants.c` / `quants.h` — 量化格式（IQ2_XXS / Q2_K / Q4_K / Q8_0）的实现（对应 `u3-l4`）；
- `imatrix/` — 收集 routed-MoE imatrix 的脚本与校准语料；
- `quality-testing/` — 把本地 GGUF 对照官方 DeepSeek 续写打分；
- `mixed/` — 拼接混合精度专家层的 Python 脚本。

`tests/` 下的 C 测试由根 `Makefile` 的 `test` 目标驱动（见 4.3.3），主要文件是 `ds4_test.c`（引擎/服务器测试）、`ds4_agent_test.c`（agent 测试）、`test_q4k_dot.c`（Q4_K 点积校验）；`test-vectors/` 子目录存放官方与本地 golden 向量（对应 `u11-l3`）。

#### 4.2.4 代码实践

**实践目标**：用一条命令把所有子目录的「文件清单」打出来，验证上面说的「一种后缀一个目录」的规律。

**操作步骤**：

```sh
# 列出每个子目录下的文件（只读，安全）
ls metal/ rocm/ gguf-tools/ tests/ speed-bench/ dir-steering/ misc/
```

**需要观察的现象**：

- `metal/` 里几乎全是 `.metal`，且文件名按算子分类（`flash_attn` / `dsv4_kv` / `moe` / `norm` / `glu` / `softmax` / `cpy` / `concat` …）。
- `rocm/` 里全是 `ds4_rocm_*.cuh`，命名高度一致。
- `gguf-tools/` 里既有 `.c`（`deepseek4-quantize.c`、`quants.c`）又有 `.py`（`collect_official.py`、`compare_scores.py`）还有子目录（`imatrix/`、`quality-testing/`、`mixed/`）——说明模型构建期工具是 C 与 Python 混合的。
- `tests/` 里既有 `.c` 也有 `.py` 和 `.txt`（长上下文提示词）。
- `misc/` 里只有几个 `.md`——对应 `AGENT.md` 说的「ignored notes, experiments, old planning material」，学习时可以**最后看**。

**预期结果**：你能在脑海里把子目录按「Metal 内核 / ROCm 内核 / 离线模型工具 / 测试 / 基准资料 / 引导实验 / 旧笔记」对号入座。

> 待本地验证：`misc/` 的具体文件可能随版本增减，不影响本讲的分类结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Metal 内核放在 `metal/*.metal` 而 ROCm 内核放在 `rocm/*.cuh`，两者「集成方式」不同？

**参考答案**：Metal 的 `.metal` 是**运行时编译**的 GPU 源（由 `ds4_metal.m` 在程序启动时交给 Metal 框架编译，因此 macOS 上要从项目树找到这些文件，才有 `--chdir` 选项）；ROCm 的 `.cuh` 是**编译期 include** 的 C++ 头，在构建 `ds4_rocm.o` 时就被 HIP 编译器静态编进二进制。所以前者是「外置资源」，后者是「内置代码」。

**练习 2**：`gguf-tools/` 的工具会在你运行 `./ds4` 推理时被调用吗？

**参考答案**：不会。`gguf-tools/` 是**离线模型构建期**工具——在你开始推理之前，用它（或外部 llama.cpp 流程）把原始权重加工成 ds4 专用的 GGUF。运行期 `./ds4` 只读取已经做好的 GGUF。根 `Makefile` 默认也**不**编译 `gguf-tools/`，它有自己的 `gguf-tools/Makefile`。

**练习 3**：`AGENT.md` 说 `misc/` 是「ignored notes, experiments, and old planning material」。学习时应如何对待它？

**参考答案**：把它视为**最低优先级**材料，甚至可以暂时忽略。它不是权威文档，内容可能过时或只是早期实验记录。遇到根 `README.md` / `AGENT.md` 与 `misc/` 冲突时，以前两者为准。

---

### 4.3 辅助文档与脚本

#### 4.3.1 概念说明

除了源码，根目录还有一批「文档」和「脚本」。它们不参与编译，但决定了你能否**正确地构建、运行和理解**这个项目：

- **文档**：`README.md`（主入口）、`AGENT.md`（工程约束与目录速览）、`CONTRIBUTING.md`（贡献规范）、`QA_BEFORE_RELEASES.md`（发布前检查清单）、`MODEL_CARD.md`（模型卡）、`STRIXHALO.md`（ROCm 平台说明）、`LICENSE`（含 GGML 版权保留）。
- **构建脚本**：`Makefile`（根）与 `gguf-tools/Makefile`（子项目）。
- **下载脚本**：`download_model.sh`（拉取 GGUF）。
- **配置**：`.gitignore` 等。

`README.md` 还专门列了一份「子文档索引」，把分散各处的子 README 串起来——这是避免你在子目录里迷路的好工具。

#### 4.3.2 核心流程

三类辅助文件的分工可以这样记：

```
文档        告诉你「为什么这么做、怎么用、怎么贡献」  → 读
Makefile    告诉你「源码怎么变成二进制」            → 跑（make ...）
download_model.sh  告诉你「模型权重从哪来」          → 跑（./download_model.sh ...）
```

`Makefile` 的一个**反直觉**行为值得特别记住：在 Linux 上直接敲 `make`（不带参数）**不会构建任何东西**，只打印一份帮助；真正的构建目标是 `make cuda-spark` / `make cuda-generic` / `make strix-halo` / `make cpu`。在 macOS 上 `make` 才会直接构建 Metal 版。这个差异写在 `Makefile` 的平台分支里。

#### 4.3.3 源码精读

先看 `README.md` 顶部的后端清单与致谢，它给整个项目定了调：

[README.md:14-21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L14-L21) — 列出三大支持后端（Metal 主目标、NVIDIA CUDA/DGX Spark、Strix Halo/ROCm），并声明「没有 llama.cpp 和 GGML 就没有这个项目」。这与 `u1-l1` 讲的「格式与数学借鉴、架构与工程自建」一脉相承。

`README.md` 还维护了一份「子文档索引」，是导航子目录的第一站：

[README.md:76-91](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L76-L91) — 列出 `CONTRIBUTING.md`、`gguf-tools/README.md`、`gguf-tools/imatrix/README.md`、`gguf-tools/quality-testing/README.md`、`dir-steering/README.md`、`speed-bench/README.md`、`tests/test-vectors/README.md` 各自讲什么。要查子目录的细节，从这里跳转最稳。

`Makefile` 的「Linux 默认只打印帮助」行为，来源是这两段：

[Makefile:80-91](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L80-L91) — 非 macOS 分支里 `all: help`（第 80 行），`help` 目标（第 82 行起）打印 `cuda-spark` / `cuda-generic` / `cuda CUDA_ARCH=` / `strix-halo` / `rocm` / `cpu` / `test` / `clean` 这些选项。对比 macOS 分支的 `all: ds4 ds4-server ds4-bench ds4-eval ds4-agent`（[Makefile:46](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L46)），就能理解为什么「同样是 `make`，两个平台行为不同」。

`Makefile` 的 `test` 目标把测试体系串起来：

[Makefile:234-241](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L234-L241) — `make test` 依次构建并运行 `ds4_test`、`ds4_agent_test`、`ds4-eval`（先跑 `--self-test-extractors`）、`q4k-dot-test`。这条命令是后面 `u11-l3`（测试向量）和 `u11-l4`（贡献与 QA）的入口。

下载脚本 `download_model.sh` 负责把模型权重拉到本地（`u1-l5` 会详讲）：

[download_model.sh](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh) — 从 HuggingFace 拉取指定档位的 GGUF，存到 `./gguf/`，并把 `./ds4flash.gguf` 软链/指向选中的主模型。它支持断点续传（`curl -C -`）。

#### 4.3.4 代码实践

**实践目标**：通过 `--help` 输出与 `Makefile` 行为，验证「文档/脚本如何对应到二进制」。

**操作步骤**：

1. 在 macOS 上看 `make` 会构建哪些二进制（Linux 用户只看 `make` 打印的帮助即可）：
   ```sh
   make           # macOS：构建 ds4 / ds4-server / ds4-bench / ds4-eval / ds4-agent
   ```
   若在 Linux，改成阅读它打印的帮助文本，确认 `cuda-spark` / `cuda-generic` / `strix-halo` / `cpu` 这几个目标的存在。
2. （可选，需要已构建）查看任一二进制的帮助：
   ```sh
   ./ds4 --help
   ./ds4-server --help
   ```
   对照 [ds4_help.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c)，你会发现所有二进制的 `--help` 文本都出自这同一个文件。

**需要观察的现象**：

- macOS 上一次 `make` 产出 **5 个**二进制：`ds4`、`ds4-server`、`ds4-bench`、`ds4-eval`、`ds4-agent`——正好对应 4.1 讲的「五个前端」。
- Linux 上 `make` **不构建**，只引导你选后端目标——这与 [Makefile:80-91](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L80-L91) 完全一致。
- `./ds4 --help` 与 `./ds4-server --help` 的说明文字都集中在 `ds4_help.c`，而不是各自散落在前端文件里——这是一种「公共文案集中管理」的小工程习惯。

**预期结果**：你能脱口而出「`make` 在两个平台行为不同」「项目有 5 个二进制」「`--help` 文案集中在 `ds4_help.c`」这三件事。

> 待本地验证：`./ds4 --help` 需要先成功构建；在没有 GPU/模型的机器上，可只读 `ds4_help.c` 来了解选项。

#### 4.3.5 小练习与答案

**练习 1**：同事在 Linux 上抱怨「敲了 `make` 什么都没编译」。请用 `Makefile` 解释原因并给出正确做法。

**参考答案**：Linux 分支里 `all` 目标定义为 `all: help`（[Makefile:80](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L80)），所以 `make` 只打印帮助。原因是 Linux 上必须先选 GPU 后端：DGX Spark/GB10 用 `make cuda-spark`，普通本地 CUDA 显卡用 `make cuda-generic`，Strix Halo 用 `make strix-halo`（= `make rocm`），纯 CPU 诊断用 `make cpu`。显式指定架构还可以 `make cuda CUDA_ARCH=sm_120`。

**练习 2**：你想了解 `quality-testing/` 子目录具体怎么给 GGUF 打分，应该先读哪个文件？

**参考答案**：先读 `README.md` 的子文档索引（[README.md:84-85](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L84-L85)），它会指向 [gguf-tools/quality-testing/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/README.md)。从顶层索引跳转比直接进子目录翻文件更稳。

**练习 3**：`download_model.sh` 下载的文件被放到哪里、默认主模型路径是什么？

**参考答案**：文件存到 `./gguf/`，并更新 `./ds4flash.gguf` 指向选中的主模型（见 README「Model Weights」一节）。`./ds4flash.gguf` 是 `ds4` / `ds4-server` 共同的默认模型路径，可用 `-m` 改写。

---

## 5. 综合实践

把三个最小模块串起来，完成下面这张**完整的源码地图表**（这是本讲的正式实践任务）。要求：

1. **运行命令采集事实**（只读，安全）：
   ```sh
   # 核心源文件行数
   wc -l ds4.c ds4.h ds4_server.c ds4_agent.c ds4_distributed.c \
          ds4_metal.m ds4_cuda.cu ds4_rocm.cu ds4_ssd.c \
          ds4_kvstore.c ds4_eval.c ds4_bench.c ds4_cli.c \
          ds4_help.c ds4_web.c ds4_gpu.h rax.c

   # 子目录文件清单
   ls metal/ rocm/ gguf-tools/ tests/ speed-bench/ dir-steering/ misc/
   ```
2. **制作一张表**，至少覆盖题目点名的 5 个文件：`ds4.c`、`ds4_server.c`、`ds4_agent.c`、`ds4_distributed.c`、`ds4_metal.m`，列出：
   - 行数量级（实测）；
   - 一句话职责（用自己的话写，不要照抄注释）；
   - 它属于「引擎核心 / GPU 后端 / 前端 / 辅助库」中的哪一类；
   - 它最终被链进哪个（些）二进制。
3. **画一张依赖草图**：标出 `ds4.h` 如何被各前端 include、`CORE_OBJS` 如何把引擎核心与某个后端 `.o` 组合、五个前端 `.o` 如何各自加上 `CORE_OBJS` 变成五个二进制。可以参考本讲 4.1.2 的文字图。
4. **挑战项**：打开 [AGENT.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md) 的 `Layout` 一节，找出它在「核心文件分工」上**没有**列出、但确实存在且重要的文件（提示：`ds4_agent.c`、`ds4_distributed.c`、`ds4_ssd.c`、`ds4_kvstore.c`、`ds4_web.c` 都不在那份清单里）。这会让你体会到 `AGENT.md` 自己说的「This list is not complete」。

**验收标准**：随便指一个根目录下的 `.c` 文件，你都能不查资料地说出它属于哪一类、大概多大、被哪个二进制使用——这张「地图」建好，后续每一讲你都能快速定位。

> 待本地验证：本实践不依赖模型或 GPU，任何能访问源码的环境都可完成；行数与子目录内容以你本地 `git` 检出版本为准。

## 6. 本讲小结

- ds4 采用**平铺式**布局，根目录的 `.c` / `.m` / `.cu` / `.h` 可分为三类：**引擎核心**（`ds4.c` 等）、**GPU 后端**（`ds4_metal.m` / `ds4_cuda.cu` / `ds4_rocm.cu`，共用 `ds4_gpu.h`）、**前端二进制**（`ds4_cli` / `ds4_server` / `ds4_agent` / `ds4_eval` / `ds4_bench`）。
- 关键结构：每个二进制 = 「自己的前端 `.o` + 公共辅助 `.o` + 一组共享的 `CORE_OBJS`」；`CORE_OBJS` = `ds4.o` + `ds4_distributed.o` + `ds4_ssd.o` + 一个后端 `.o`（Metal/CUDA/ROCm 任选其一）。CPU 构建用 `-DDS4_NO_GPU` 把 `ds4.c` 编成 `ds4_cpu.o`，不带任何后端对象。
- 文件大小有指导意义但也有反直觉：`ds4.c` 与 `ds4_metal.m` 各约 2.7 万行（最大）；而 `ds4_rocm.cu`（约 130 行）和 `ds4_ssd.c`（约 180 行）虽小，功能却藏在 `rocm/*.cuh` 与 `ds4.c` 主体里。
- 子目录按文件类型归拢：`metal/`（运行时编译的 Metal 内核）、`rocm/`（编译期 include 的 ROCm 头）、`gguf-tools/`（**离线**模型构建工具）、`tests/`、`speed-bench/`、`dir-steering/`、`misc/`（低优先级旧笔记）。
- 三类辅助文件各司其职：文档（`README.md` / `AGENT.md` / `CONTRIBUTING.md` 等）负责「为什么和怎么用」；`Makefile` 负责「怎么构建」（注意 Linux 上 `make` 只打印帮助）；`download_model.sh` 负责「模型从哪来」。
- `AGENT.md` 的 `Layout` 一节是目录速览的最佳入口，但它自己声明「不完整」——真正完整的地图要靠 `Makefile` 的链接关系和每个文件头部的自述注释来补齐。

## 7. 下一步学习建议

地图已经建好，接下来分两条路走：

- **想立刻把项目跑起来**：进入 `u1-l4`（构建系统与多后端编译），深入 `Makefile` 的平台分支与 `DS4_NO_GPU` 开关；然后 `u1-l5`（下载模型与首次运行）用 `download_model.sh` 拉模型、跑通第一次推理。
- **想先理解引擎 API 边界**（推荐想读源码的读者）：直接跳到 `u2-l1`（`ds4.h`：引擎边界与生命周期），本讲提到的 `ds4_engine` / `ds4_session` / `ds4_gpu_tensor` 会在那里正式展开。后续 `u3` 会进入 `ds4.c` 的模型加载与权重绑定，那时你会庆幸现在已经有了一张源码地图。

建议继续阅读的源码入口：[AGENT.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md)（通读一遍工程约束）、[ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h)（通读公共 API）、以及 `Makefile` 全文（理解完整依赖图）。
