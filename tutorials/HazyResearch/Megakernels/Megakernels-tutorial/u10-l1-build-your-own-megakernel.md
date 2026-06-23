# 用 mk_init 搭建你自己的 megakernel

## 1. 本讲目标

本讲面向已经理解 megakernel 基本原理、想动手写出「自己的第一个 megakernel」的读者。学完后你应当能够：

1. 用 `util/mk_init/main.py` 这个交互式脚手架，在几秒内生成一个**可编译、可运行**的 megakernel 项目骨架。
2. 读懂脚手架生成的 `.cu` 文件里 `TestOp` 的**五子结构**（controller / loader / launcher / consumer / storer），并说清楚每一子结构在 megakernel 虚拟机（VM）里由哪一类 warp 调用、解决什么问题。
3. 读懂脚手架生成的 `setup.py`，说清楚它如何把环境变量、nvcc 编译选项、include 目录拼成一条编译命令，从而把一个 `.cu` 文件编译成 Python 可导入的 `.so`。
4. 亲手修改 `TestOp::loader`，让它打印自定义信息，并准确列出编译这个项目所需的**全部环境变量**。

本讲只生成一篇文件：`Megakernels-tutorial/u10-l1-build-your-own-megakernel.md`，不会改动任何源码。

---

## 2. 前置知识

在进入脚手架之前，先用最通俗的方式把几个名词讲清楚。

### 2.1 什么是 megakernel

普通 CUDA kernel 是「一次调用干一件事」：你 launch 一个 kernel 做矩阵乘，再 launch 另一个做激活，每次 launch 都要付出启动开销。Megakernels 的核心思想是：**写一个常驻 GPU 的虚拟机（VM）kernel**，它启动后就一直跑，靠读取一串「指令（instruction）」来决定每一步干什么——更像 CPU 在执行机器码，而不是传统 GPU kernel。

这样做的好处是：把一连串小算子（比如 LLM 推理里的 RMSNorm、QKV、Attention、MLP）**折叠进一个常驻 kernel**，省掉大量 launch 开销，对低延迟推理非常关键。

### 2.2 指令、opcode 与 op

VM 每次从全局内存读一条指令（一个固定宽度的 `int` 数组）。指令的第一个整数是 **opcode**（操作码）。框架里每个「op」就是一个 `struct`，带一个 `static constexpr int opcode`，以及若干子结构。VM 根据 opcode 在指令流里查表，找到对应的 op，再调用它的子结构去真正干活。

opcode `0` 永远留给 `NoOp`（空操作），框架会自动把它插到指令流最前面，保证 VM 能正确处理「什么都不做」的指令。

### 2.3 warp 与五类工人

一个 megakernel block 里有若干个 warp（线程束），被划分成几类「工人」：

| warp 类型 | 数量 | 职责（直觉版） |
|-----------|------|----------------|
| controller | 1 | 「调度员」：取指令、分配物理页、构造信号量 |
| loader | 1 | 「搬运工 A」：把数据从显存搬进 shared memory |
| storer | 1 | 「搬运工 B」：把结果从 shared memory 搬回显存 |
| launcher | 1 | 「发射台」：触发张量核心（tensor core）运算、管理页面生命周期 |
| consumer | N | 「干活的」：真正执行计算（比如 mma），可配置占用更多寄存器 |

这五类工人就是 `TestOp` 里那五个子结构的来源。如果你还没看过 megakernel VM 的整体执行流程，建议先复习前置讲义 **u8-l1**；`setup.py` 的构建背景可参考 **u5-l2**。

---

## 3. 本讲源码地图

本讲涉及的文件分为两类：**脚手架模板**（你直接读、直接改的就是它们）和**框架内核**（模板生成的代码会去 `#include` 它们，本讲只读不写）。

### 3.1 脚手架模板（`util/mk_init/`）

| 文件 | 作用 |
|------|------|
| [util/mk_init/main.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py) | 脚手架入口脚本，类似 `npm init`。交互式问项目名、建目录、拷模板、替换占位符。 |
| [util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu) | 被拷贝成 `src/<项目名>.cu` 的源码模板，里面有 `globals`、`TestOp`、`PYBIND11_MODULE`。 |
| [util/mk_init/sources/setup.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py) | 被原样拷贝的构建脚本，用 nvcc 把 `.cu` 编译成 Python 扩展。 |
| [util/mk_init/sources/src/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/config.cuh) | 被拷贝成 `src/config.cuh`，定义 `<项目名>_config` 这个配置结构体。 |
| [util/mk_init/sources/tests/test_example.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/tests/test_example.py) | 一个最小 Python 调用示例：构造指令张量、调用编译出来的 kernel。 |
| [util/mk_init/sources/README.md](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/README.md) | 拷给用户的说明，给出构建命令示例。 |

> 注意：模板文件名里的 `{{PROJECT_NAME_LOWER}}` 是**占位符**，不是合法文件名。生成项目时它会被替换成你的项目名（小写）。

### 3.2 框架内核（`include/`，只读）

| 文件 | 作用 |
|------|------|
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | 定义真正的 VM kernel `mk`，以及 warp 分派逻辑。 |
| [include/util.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh) | `state`（VM 状态）、`dispatch_op`（opcode 分派）、`MAKE_WORKER` 宏。 |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | 框架自带 `default_config`，以及 `instruction_layout` / `timing_layout` 类型别名。 |
| [include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) | `NoOp`（opcode 0），所有 op 列表的最前置成员。 |
| [include/controller/](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | controller warp 的主循环，会回调 op 的 `init_semaphores` / `release_lid`。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

- **4.1 mk_init 脚本流程**：`main.py` 怎么把模板变成项目。
- **4.2 TestOp 模板与五子结构**：`.cu` 里的 `TestOp` 如何对接框架 VM。
- **4.3 setup.py / build_ext 构建**：nvcc 怎么被调用。

---

### 4.1 mk_init 脚手架流程

#### 4.1.1 概念说明

`mk_init` 做的事和 `npm init`、`cargo new` 一样：**用一个脚本把你从零配置中解放出来**。一个能编译的 megakernel 项目至少需要：源码 `.cu`、配置 `config.cuh`、构建脚本 `setup.py`、调用示例 `test_example.py`，外加目录结构和 `.gitignore`。手写这些既繁琐又容易抄错。`mk_init` 把一份「带占位符的模板」放在 `sources/` 下，运行时把占位符替换成你的项目名再写到目标目录。

占位符一共有三个，统一在 [util/mk_init/main.py:23-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L23-L28) 里处理：

- `{{PROJECT_NAME_LOWER}}` → 项目名小写（用于文件名、Python 模块名、config 名）。
- `{{PROJECT_NAME_UPPER}}` → 项目名大写。
- `{{PROJECT_NAME}}` → 原样项目名。

#### 4.1.2 核心流程

`main()` 的执行流程可以概括成下面这个伪代码：

```text
解析命令行参数 (--name, --target)
├─ 若无 --name，交互式 prompt 询问项目名
├─ 校验项目名：只允许字母/数字/下划线/连字符
├─ 确定目标目录：默认 <当前目录>/<项目名>
├─ 若目录已存在且非空，询问是否覆盖
├─ 建目录 src/ 和 tests/
├─ 逐个拷贝模板文件（拷贝时替换占位符）：
│    setup.py, README.md, tests/test_example.py,
│    src/config.cuh, src/<项目名>.cu
├─ 写一个 .gitignore
└─ 打印 "Next steps" 提示
```

要被拷贝的文件清单写死在 [util/mk_init/main.py:122-128](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L122-L128)。注意里面**没有 Makefile**——这是一个后面会专门提醒的「坑」。

#### 4.1.3 源码精读

**入口与参数解析** —— [util/mk_init/main.py:66-84](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L66-L84)：用 `argparse` 提供 `--name` 和 `--target`；如果没给 `--name` 就走交互式输入；然后用一行字符串校验保证项目名只含字母、数字、下划线、连字符。

**占位符替换** —— [util/mk_init/main.py:23-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L23-L28) 是整套脚手架的「心脏」：三个 `str.replace`，把模板里的 `{{...}}` 换成真实项目名。因为 `replace_placeholders` 也被用在 `copy_template_file` 里对**文件名**做替换（见第 33 行），所以连 `src/{{PROJECT_NAME_LOWER}}.cu` 这种带占位符的文件名也能被正确改写成 `src/<项目名>.cu`。

**单个文件拷贝** —— [util/mk_init/main.py:30-50](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L30-L50)：读模板全文 → 替换占位符 → 写到目标路径。逻辑非常直白，没有依赖第三方库。

**目录结构创建** —— [util/mk_init/main.py:53-63](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L53-L63)：只建 `src/` 和 `tests/` 两个目录。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次脚手架，看看它到底生成了什么。

**操作步骤**：

1. 在仓库根目录运行（`--name` 直接给名字，跳过交互）：

   ```bash
   python util/mk_init/main.py --name MyFirstMK --target /tmp/myfirstmk
   ```

2. 查看生成结果：

   ```bash
   find /tmp/myfirstmk -type f
   ```

**需要观察的现象**：

- 终端会逐行打印 `Copying ... ✓ Created ...`。
- `find` 应该列出：`setup.py`、`README.md`、`.gitignore`、`tests/test_example.py`、`src/config.cuh`、`src/myfirstmk.cu`。
- **关键观察**：模板里那个非法文件名 `{{PROJECT_NAME_LOWER}}.cu` 已经变成了 `src/myfirstmk.cu`；打开它，里面所有 `{{PROJECT_NAME_LOWER}}` 都变成了 `myfirstmk`，`myfirstmk_config` 这个名字也正确出现在 `config.cuh` 和 `.cu` 里。

**预期结果**：生成的目录与上一节「核心流程」列出的清单一致。

**待本地验证**：如果你不带 `--target`，脚本会把项目建在 `当前目录/MyFirstMK`，且该目录非空时会交互式询问是否继续——这一点建议自己试一次以体会 [util/mk_init/main.py:93-97](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L93-L97) 的覆盖保护逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么模板文件名能写成 `{{PROJECT_NAME_LOWER}}.cu` 这样「非法」的形式？
**答案**：因为 `copy_template_file` 在写出之前，先用 `replace_placeholders` 对**文件名字符串**做了一次占位符替换（[main.py:33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L33)），真正落到磁盘上的文件名已经是替换后的合法名字。

**练习 2**：如果项目名传成 `My MK!`（带空格和感叹号），脚本会怎样？
**答案**：会在 [main.py:82-84](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L82-L84) 的校验处报错退出，因为去掉 `_` 和 `-` 后 `My MK!` 仍不是纯字母数字（`!` 和空格不合法）。

---

### 4.2 TestOp 模板与五子结构

#### 4.2.1 概念说明

脚手架生成的 `.cu` 文件是整个项目的「主源码」。它的核心是一个叫 `TestOp` 的 op，以及一个把 op 注册给 Python 的 `PYBIND11_MODULE`。`TestOp` 是一个**最小可运行的 op**：它不怎么算东西，只是让 loader 打印一行 `Hello, world`，用来证明「VM 真的跑起来了」。

理解 `TestOp` 的关键是理解它的**五子结构**：`controller` / `loader` / `launcher` / `consumer` / `storer`。这五个名字不是随便起的，而是和第 2.3 节那五类 warp 一一对应——每类 warp 在自己的主循环里，会去调用 op 里同名子结构的 `run`（controller 略有不同，下面单独说）。

一句话总结 VM 的运行模型：**一个常驻 kernel，内部五类 warp 各跑各的主循环，每个主循环都遍历同一串指令，按 opcode 找到对应 op，再调用 op 对应子结构去干活。**

#### 4.2.2 核心流程

**指令分派（opcode dispatch）** 是把 op 和 warp 串起来的机制。框架用一个模板递归结构 `dispatch_op` 在**编译期**生成一棵「if opcode == X 则调用 opX」的查找树。设指令流里出现的 opcode 集合为 \(\{o_0, o_1, \dots, o_{n-1}\}\)，则分派逻辑等价于：

\[
\text{dispatch}(o) = \begin{cases} \text{op}_0.\text{子结构}.\text{run} & o = o_0 \\ \text{op}_1.\text{子结构}.\text{run} & o = o_1 \\ \vdots \\ \text{trap} & o \notin \{o_i\} \end{cases} \]

注意第 4 行：找不到匹配 opcode 时直接 `trap`（[util.cuh:38](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L38)），意思是「这是非法指令，直接让 GPU 崩掉」，这是一种 fail-fast 的防御式设计。

**warp 分派** 发生在 kernel 入口。整个 block 的线程按 warp 编号分流：

```text
mk_internal(g):
  if warpid() < NUM_CONSUMER_WARPS:
      consumer::main_loop(...)        # 所有 consumer warp
  else:
      switch warpgroup::warpid():
        case 0: loader::main_loop(...)
        case 1: storer::main_loop(...)
        case 2: launcher::main_loop(...)
        case 3: controller::main_loop(...)
```

每个 `main_loop` 的内部结构由 `MAKE_WORKER` 宏统一生成，骨架是：

```text
for 每条指令 instruction_index in [0, num_iters):
    await_instruction()                 # 等待 controller 把这条指令准备好
    opcode = instruction()[0]           # 取操作码
    dispatch_op 找到 opcode 对应的 op
    调用 op::<本worker名字>::run(g, mks)  # 真正干活
    next_instruction()                  # 推进到下一条
```

这里有一个很容易忽略的细节：**`NoOp`（opcode 0）总是被自动插到 op 列表最前面**。在 [include/megakernel.cuh:159-164](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L159-L164) 里可以看到，`megakernel_wrapper::run` 调用 `mk_internal` 时，手动把 `NoOp<config>` 拼到了 `ops...` 的最前面。所以即使你只写了一个 `TestOp`，实际参与分派的 op 列表是 `<NoOp, TestOp>`。

#### 4.2.3 源码精读

先看模板 `.cu` 的全貌，它分成三块：`globals`、`state` 类型别名、`TestOp`，最后是 `PYBIND11_MODULE`。

**globals：每个 block 共享的「全局输入」** —— [util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L11-L19)：

```cpp
struct globals {
    using instruction_layout = megakernel::instruction_layout<myfirstmk_config>;
    using timing_layout      = megakernel::timing_layout<myfirstmk_config>;
    instruction_layout instructions;
    timing_layout timings;
    dim3 grid() { return dim3(148); }
    dim3 block() { return dim3(myfirstmk_config::NUM_THREADS); }
    int dynamic_shared_memory() { return myfirstmk_config::DYNAMIC_SHARED_MEMORY; }
};
```

- `instructions` / `timings` 是两个张量（`kittens::gl<...>`），分别承载「指令流」和「计时数据」。它们的类型来自框架的 [include/config.cuh:53-56](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L53-L56)。
- `grid()` / `block()` / `dynamic_shared_memory()` 三个函数告诉 Python 绑定层：kernel 要用多少个 block、多少线程、多少动态 shared memory。这里 grid 写死成 148 个 block。

> `globals` 就是「这个 op 需要从外部世界读哪些张量」的声明。真实项目（如 LLM demo）会在 `globals` 里塞进权重、KV cache 等几十个张量，见 [demos/low-latency-llama/llama.cu:28-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L49) 的对比。

**TestOp 的五子结构** —— [util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu:23-59](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L23-L59)。逐个看：

1. `controller`（[L25-32](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L25-L32)）：注意它**没有 `run`**，而是提供 `init_semaphores` 和 `release_lid`。controller warp 不会像其他四类那样每条指令调一次 `run`；它通过两个专门的分派器回调这两个函数：
   - `init_semaphores` 由 [include/controller/semaphore_constructor.cuh:13-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/semaphore_constructor.cuh#L13-L18) 调用，用来声明「这条指令需要几个动态信号量」。`TestOp` 返回 `0`，表示不需要。
   - `release_lid` 由 [include/controller/page_allocator.cuh:13-18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/page_allocator.cuh#L13-L18) 调用，用来回答「物理页分配」问题。`TestOp` 直接把传入的 `query` 原样返回。

2. `loader`（[L33-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L33-L37)）：本讲的「主角」。只在 `laneid() == 0` 时打印 `Hello, world`。这就是你后面要改的地方。

   ```cpp
   struct loader {
       static __device__ void run(const globals &g, state &s) {
           if(laneid() == 0) { printf("Hello, world from myfirstmk!\n"); }
       }
   };
   ```

3. `launcher`（[L38-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L38-L52)）：负责等页面就绪并释放页面（`wait_page_ready` / `finish_page`）。这是「页面生命周期」管理——页面是 shared memory 里被划分出来的固定大小数据块，loader 装数据、consumer 用数据、launcher 协调谁先谁后。`#ifdef KITTENS_BLACKWELL` 那段是 Blackwell 架构专属的张量就绪同步。

4. `consumer`（[L53-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L53-L55)）：空的。真实 op 里这里会写 mma / 张量核心计算。

5. `storer`（[L56-58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L56-L58)）：空的。真实 op 里这里会把结果写回显存。

**为什么这五个 `run` 会被正确调用？** 看框架的 `MAKE_WORKER` 宏，[include/util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304)。它为 `loader`/`storer`/`launcher`/`consumer` 各生成一个 `main_loop`，核心一行是第 288-291 行的 `dispatch_op<...>::run(...)`，最终落到第 269 行 `op::name::run(g, mks)`——也就是「调用 op 里和本 worker 同名的子结构的 `run`」。`name` 由宏参数拼接而来，所以 `MAKE_WORKER(loader, ...)` 生成的代码会去调 `op::loader::run`，正好匹配 `TestOp::loader`。

**Python 绑定** —— [util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu:61-66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L61-L66)：

```cpp
PYBIND11_MODULE(myfirstmk, m) {
    m.doc() = "";
    kittens::py::bind_kernel<megakernel::mk<myfirstmk_config, globals, TestOp>>(
        m, "example_megakernel",
        &globals::instructions,
        &globals::timings);
}
```

- `PYBIND11_MODULE(myfirstmk, m)` 把这个 `.so` 注册成名为 `myfirstmk` 的 Python 模块。
- `bind_kernel<megakernel::mk<...>>` 把真正的 VM kernel `mk`（定义在 [include/megakernel.cuh:166-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L166-L171)）包装成一个 Python 可调用对象，名字叫 `example_megakernel`。
- 后面两个 `&globals::instructions` / `&globals::timings` 告诉绑定层：调用时要按顺序传入这两个张量。

> `bind_kernel` 本身定义在依赖库 ThunderKittens 的 `pyutils/pyutils.cuh` 里（由环境变量 `THUNDERKITTENS_ROOT` 指向），不在本仓库中。它的工作是：接收 Python 传来的 torch 张量，按 `grid()`/`block()`/`dynamic_shared_memory()` 配置好 launch 参数，再真正 launch 这个 kernel。

**调用侧** —— [util/mk_init/sources/tests/test_example.py:1-9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/tests/test_example.py#L1-L9)：

```python
from myfirstmk import example_megakernel
instruction = torch.zeros(148, 1, 32, dtype=torch.int32, device="cuda")
timing      = torch.zeros(148, 1, 128, dtype=torch.int32, device="cuda")
instruction[0, 0, 0] = 1     # 把第 0 条指令的 opcode 设成 1
example_megakernel(instruction, timing)
```

`instruction[0,0,0] = 1` 这一行是整个示例的「开关」：它把第一条指令的 opcode 写成 `1`，而 `TestOp::opcode == 1`（[L24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L24)），于是 VM 处理到这条指令时分派到 `TestOp`，loader 打印那行 `Hello, world`。

#### 4.2.4 代码实践

**实践目标**：修改 `TestOp::loader`，让它打印你的自定义信息，并理解打印会触发几次。

**操作步骤**：

1. 打开 4.1.4 生成的 `/tmp/myfirstmk/src/myfirstmk.cu`。
2. 把 loader 改成下面这样（示例代码）：

   ```cpp
   struct loader {
       static __device__ void run(const globals &g, state &s) {
           if(laneid() == 0) {
               printf("Hello from block %d, my name is MyFirstMK!\n", blockIdx.x);
           }
       }
   };
   ```

3. 暂不编译也没关系——这一步先做**源码阅读型验证**：对照 [include/util.cuh:260-304](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L260-L304) 的 `MAKE_WORKER`，确认 loader warp 的主循环确实会调用 `TestOp::loader::run`。

**需要观察的现象（编译运行后）**：因为 `grid()` 返回 148 个 block，每个 block 的 loader 在处理到 opcode==1 的那条指令时都会打印一次，所以**预期会看到多条 `Hello from block X ...`**，X 取值从 0 递增。

**预期结果 / 待本地验证**：直观上每个 block 打印一次，故总行数与参与处理的 block 数相关（最朴素的理解是接近 148 行）；但精确打印次数取决于每个 block 实际遍历多少条 opcode==1 的指令——这一精确计数**待本地验证**。无论次数多少，你能稳定看到自定义字符串被打印，就说明 op 已被正确分派。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TestOp::controller` 没有 `run` 函数？
**答案**：controller warp 不走 `MAKE_WORKER` 那套「每条指令调一次 `run`」的循环。它有自己专门的主循环 [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh)，只通过 `init_semaphores` 和 `release_lid` 两个回调与 op 交互。

**练习 2**：如果把 `test_example.py` 里的 `instruction[0, 0, 0] = 1` 改成 `= 2`，会发生什么？
**答案**：opcode 变成 2。编译期参与分派的 op 是 `<NoOp(opcode 0), TestOp(opcode 1)>`，没有 opcode==2 的 op，于是 `dispatch_op` 一路递归到基类的 `trap`（[util.cuh:38](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L38)），运行时会触发 GPU 异常。

**练习 3**：`bind_kernel` 的模板参数 `megakernel::mk<config, globals, TestOp>` 里，`TestOp` 是唯一的 op 吗？
**答案**：不是。`megakernel_wrapper` 在内部会把 `NoOp<config>` 自动拼到最前面（[megakernel.cuh:162](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L162)），所以实际分派的 op 列表是 `<NoOp, TestOp>`，opcode 0 永远由 NoOp 处理。

---

### 4.3 setup.py / build_ext 构建

#### 4.3.1 概念说明

`setup.py` 解决的问题是：**把一个 `.cu` 文件编译成 Python 能 `import` 的 `.so`**。普通 Python 包用 `setuptools` 调 C 编译器；这里不行，因为源码是 CUDA，必须用 **nvcc**。所以模板里的 `setup.py` 做了一个「偷梁换柱」：继承 pybind11 的 `build_ext`，重写 `build_extension`，遇到 CUDA 扩展时**不调默认编译器，而是手动拼一条 nvcc 命令**去执行。

要让这条 nvcc 命令成立，需要三样东西：**环境变量**（告诉它依赖在哪、目标 GPU 是什么）、**nvcc 编译选项**（优化等级、C++ 标准、架构等）、**include 目录**（头文件搜索路径）。下面逐一拆解。

#### 4.3.2 核心流程

`setup.py` 的执行流程：

```text
读取环境变量 → 拼 nvcc_flags → 拼 include_dirs → 拼 python 链接 flags
  → 按 TARGET_GPU 追加架构宏 → 定义 CudaExtension → setup() 调用
  → 触发 build_ext → CudaBuildExt.build_cuda_extension():
        nvcc  <源文件> <nvcc_flags> -I<include...> -o <输出.so>
```

#### 4.3.3 源码精读

**全部环境变量** —— [util/mk_init/sources/setup.py:8-13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L8-L13)。这是本模块最需要记住的一张表：

| 环境变量 | 默认值 | 含义 |
|----------|--------|------|
| `THUNDERKITTENS_ROOT` | `''`（空） | ThunderKittens 仓库根目录，提供 `include/kittens.cuh`、`include/pyutils/` 等。 |
| `MEGAKERNELS_ROOT` | `''`（空） | 本仓库（Megakernels）根目录，提供 `include/megakernel.cuh` 等框架头。 |
| `PYTHON_VERSION` | `'3.13'` | 链接的 Python 版本，用于 `-lpython3.13`。 |
| `TARGET_GPU` | `'HOPPER'` | 目标架构：`HOPPER` 或 `BLACKWELL`。 |
| `NVCC` | `'nvcc'` | nvcc 可执行文件路径。 |

此外，`setup.py` 还会**隐式依赖** PATH 上能找到的 `python`、`python3-config`（见 [L19-24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L19-L24) 和 [L64-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L64-L69)），用来探测 Python 头文件目录和链接选项。

**nvcc 编译选项** —— [util/mk_init/sources/setup.py:27-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L27-L53)。挑几个关键的讲：

- `--expt-extended-lambda` / `--expt-relaxed-constexpr`：开启 nvcc 的扩展 lambda 和宽松 constexpr， kittens 大量用到。
- `-std=c++20`：用 C++20 标准。
- `-O3 --use_fast_math`：最高优化 + 快速数学。
- `-Xptxas=--warn-on-spills`：PTX 汇编阶段若发生寄存器 spill（寄存器不够用退回显存）会警告——对调优 megakernel 很关键。
- `-shared -fPIC`：编成位置无关的共享库。
- `f'-lpython{PYTHON_VERSION}'`：链接对应版本的 Python（让 `.so` 能与 Python 解释器交互）。

**架构条件分支** —— [util/mk_init/sources/setup.py:75-80](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L75-L80)：

- `HOPPER` → 追加 `-DKITTENS_HOPPER` 和 `-arch=sm_90a`（H100/H200）。
- `BLACKWELL` → 追加 `-DKITTENS_HOPPER -DKITTENS_BLACKWELL` 和 `-arch=sm_100a`（B200）。注意 Blackwell 同时也定义了 `KITTENS_HOPPER`，所以 `.cu` 模板里 `#ifdef KITTENS_BLACKWELL` 那段才会生效。
- 其它值直接 `raise ValueError`，拒绝编译。

**include 目录** —— [util/mk_init/sources/setup.py:56-61](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L56-L61)：把 `THUNDERKITTENS_ROOT/include`、`MEGAKERNELS_ROOT/include`、pybind11 头目录、Python 头目录拼起来。这就是为什么 `THUNDERKITTENS_ROOT` / `MEGAKERNELS_ROOT` 这两个环境变量**绝对不能为空**——否则 `#include "kittens.cuh"`、`#include "megakernel.cuh"` 都会找不到。

**自定义 build_ext** —— [util/mk_init/sources/setup.py:91-121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L91-L121)。这是整套构建的「核心机关」：

```cpp
// 概念示意（非项目原码，仅说明结构）
class CudaExtension(Extension): ...           // 一个标记类，区分 CUDA 扩展

class CudaBuildExt(build_ext):
    def build_extension(self, ext):
        if isinstance(ext, CudaExtension):
            self.build_cuda_extension(ext)     // CUDA 走自定义分支
        else:
            super().build_extension(ext)       // 普通 C 走默认

    def build_cuda_extension(self, ext):
        cmd = [nvcc] + ext.sources + nvcc_flags + ['-o', ext_path]
        for d in include_dirs: cmd += ['-I', d]
        subprocess.check_call(cmd)             // 真正执行 nvcc
```

实际拼接命令的那一行在 [L112](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L112)：`cmd = [nvcc] + ext.sources + nvcc_flags + ['-o', ext_path]`，随后补上所有 `-I` 目录，最后 `subprocess.check_call(cmd)` 执行。注意它还会在第 118 行把整条命令打印出来——编译时盯着终端就能看到完整的 nvcc 调用。

> 上面的 `CudaExtension`/`CudaBuildExt` 示意是「示例代码」，仅用于说明类之间的分工；真实代码见 [setup.py:91-121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L91-L121)。

**一个重要的坑：没有 Makefile**。`main.py` 在 [L164-168](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L164-L168) 打印的「Next steps」提示用 `make` / `make test` / `make clean`，但 [L122-128](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L122-L128) 拷贝的模板文件清单里**根本没有 Makefile**。所以 `make` 命令会直接报「No rule to make target」。真正的构建方式是模板 README 给出的 [util/mk_init/sources/README.md:5-8](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/README.md#L5-L8)：用 `python setup.py install` 并带齐环境变量。这一点初学者几乎一定会踩，本讲特别提醒。

#### 4.3.4 代码实践

**实践目标**：写出编译 4.1.4 生成的项目所需的**完整命令**，并解释每个环境变量。

**操作步骤**：

1. 确认你本地有 `THUNDERKITTENS_ROOT`（ThunderKittens 仓库路径）和 `MEGAKERNELS_ROOT`（本仓库路径）。假设分别是 `~/code/ThunderKittens` 和 `~/code/Megakernels`。
2. 进入生成项目目录，运行（这是 B200 / Blackwell 的例子）：

   ```bash
   cd /tmp/myfirstmk
   TARGET_GPU=BLACKWELL \
   MEGAKERNELS_ROOT=~/code/Megakernels \
   THUNDERKITTENS_ROOT=~/code/ThunderKittens \
   python setup.py install
   ```

3. 观察终端输出。

**需要观察的现象**：

- 终端会先打印一行 `Building CUDA extension with command: nvcc src/myfirstmk.cu ...`（来自 [setup.py:118](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L118)），你能在这条命令里看到所有 nvcc_flags、`-I` 目录和 `-arch=sm_100a`。
- 随后是 nvcc 的 verbose 输出（因为 `-Xnvlink=--verbose -Xptxas=--verbose`）。
- 若发生寄存器 spill，会有 `--warn-on-spills` 触发的告警。

**预期结果 / 待本地验证**：成功时会在 site-packages（或 build 目录）下生成 `myfirstmk.cpython-XX-...so`，Python 里 `import myfirstmk` 能成功并看到 `example_megakernel`。能否真实编译**待本地验证**（需要真实的 H100/B200 环境与正确版本的 ThunderKittens）。

**纯源码阅读型替代实践**（无 GPU 时）：不执行编译，而是打开 `setup.py`，对照 [L8-13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/setup.py#L8-L13) 手写出「构建这个项目所需的全部环境变量清单」，并口述每个变量的作用——这正是本讲代码实践任务要求的一项。

#### 4.3.5 小练习与答案

**练习 1**：如果只设了 `MEGAKERNELS_ROOT` 却忘了 `THUNDERKITTENS_ROOT`，编译会卡在哪一步？
**答案**：`include_dirs` 里 `THUNDERKITTENS_ROOT/include` 会变成 `/include`，导致 `#include "kittens.cuh"`、`#include "pyutils/pyutils.cuh"` 找不到，nvcc 报「file not found」之类错误。

**练习 2**：为什么 `TARGET_GPU=BLACKWELL` 的分支里同时定义了 `KITTENS_HOPPER` 和 `KITTENS_BLACKWELL`？
**答案**：Blackwell 在 kittens/megakernels 的抽象里是「Hopper 的超集」，大量 Hopper 代码路径仍然适用，所以两个宏都定义；这样 `.cu` 里既有的 Hopper 逻辑和 `#ifdef KITTENS_BLACKWELL` 包裹的 Blackwell 专属逻辑（如模板里 launcher 的 `tensor_finished` 同步，见 [{{PROJECT_NAME_LOWER}}.cu:45-50](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu#L45-L50)）才能同时生效。

**练习 3**：为什么 `main.py` 提示的 `make` 命令不能用？
**答案**：因为脚手架拷贝的模板清单（[main.py:122-128](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L122-L128)）里没有 Makefile，生成的项目里没有 `Makefile` 这个文件，`make` 自然报「No rule to make target」。正确做法是用 `python setup.py install`。

---

## 5. 综合实践

把三个模块串起来，完成一个端到端的小任务：**从零生成、改造、理解构建一个属于自己的 megakernel 项目**。

1. **生成**：运行 `python util/mk_init/main.py --name HelloMK --target /tmp/hellomk`，确认生成了 `src/hellomk.cu`、`src/config.cuh`、`setup.py`、`tests/test_example.py`。

2. **改造 op**：打开 `src/hellomk.cu`，做两处改动（均为示例代码）：
   - 在 `TestOp::loader` 里打印你的名字和 `blockIdx.x`：
     ```cpp
     if(laneid() == 0) printf("[HelloMK] block %d reporting\n", blockIdx.x);
     ```
   - 把 `TestOp::consumer`（原本是空的）改成也打印一行：
     ```cpp
     static __device__ void run(const globals &g, state &s) {
         if(kittens::laneid() == 0 && kittens::warpid() == 0)
             printf("[HelloMK] consumer warp 0 alive\n");
     }
     ```
   （注意 `consumer` 有 16 个 warp，所以加 `warpid()==0` 限制只打印一次/block。）

3. **解释构建**：用一张表写出编译所需**全部环境变量**（`THUNDERKITTENS_ROOT`、`MEGAKERNELS_ROOT`、`PYTHON_VERSION`、`TARGET_GPU`、`NVCC`），并说明哪个变量缺失会导致哪一类编译错误。

4. **（可选，需 GPU）编译并运行**：
   ```bash
   cd /tmp/hellomk
   TARGET_GPU=HOPPER MEGAKERNELS_ROOT=<本仓库> THUNDERKITTENS_ROOT=<TK仓库> python setup.py install
   python tests/test_example.py
   ```
   预期看到 loader 和 consumer 的打印输出。精确行数与每个 block 遍历的指令数有关，**待本地验证**。

5. **复盘提问**：如果运行后**什么都没打印**，请按这个顺序排查——(a) `test_example.py` 里 `instruction[0,0,0]` 是否设成了 `1`？(b) `TestOp::opcode` 是否仍是 `1`？(c) `import` 的模块名和 `.so` 名是否一致？这三点分别对应「指令没命中 op」「opcode 不匹配」「构建产物名不对」三类最常见错误。

---

## 6. 本讲小结

- `mk_init/main.py` 是一个极简脚手架：解析项目名 → 建目录 → 拷模板 → 用三个 `str.replace` 替换 `{{PROJECT_NAME_LOWER/UPPER/NAME}}` 占位符，连文件名里的占位符也会被替换。
- 脚手架生成的 `TestOp` 拥有**五子结构**，分别对应五类 warp：controller（无 `run`，靠 `init_semaphores`/`release_lid` 回调）、loader、launcher、consumer、storer。
- 框架靠 `MAKE_WORKER` 宏为每类 warp 生成主循环，靠 `dispatch_op` 按 opcode 在**编译期**分派到对应 op；`NoOp`（opcode 0）总是被自动前置。
- `.cu` 末尾的 `PYBIND11_MODULE` 用 `kittens::py::bind_kernel<mk<...>>` 把 VM kernel 注册成 Python 可调用对象 `example_megakernel`，`globals` 声明了它需要的全部输入张量。
- `setup.py` 通过自定义 `CudaBuildExt` 把 `.cu` 编译交给 nvcc，关键输入是五个环境变量（`THUNDERKITTENS_ROOT`、`MEGAKERNELS_ROOT`、`PYTHON_VERSION`、`TARGET_GPU`、`NVCC`）。
- **坑**：`main.py` 提示用 `make`，但模板不含 Makefile；真正的构建命令是 `python setup.py install` 并带齐环境变量。

---

## 7. 下一步学习建议

- **读一个真实 op**：脚手架里的 `TestOp` 只会打印。建议接着读 [demos/low-latency-llama/](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) 下的 op（如 `attention_partial`、`rms_qkv_rope_append`），看 loader 怎么搬数据、consumer 怎么做 mma、storer 怎么写回，从而把空的 `consumer`/`storer` 填满。
- **深入 VM 内部**：本讲只点到 `MAKE_WORKER` 和 `dispatch_op`。建议顺着 [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) 的 `mk_internal` 往下读，理解指令流水线（`INSTRUCTION_PIPELINE_STAGES`）、页面（page）与信号量（semaphore）是如何协作的——这正是前置讲义 u8-l1 的延伸。
- **动手扩展 op 列表**：试着在 `bind_kernel` 里注册**两个** op（比如再加一个 `opcode==2` 的 op），构造一条 `opcode = [1, 2, 1]` 的指令流，观察 VM 如何按顺序分派并执行多操作——这是理解 megakernel「指令流水」最直接的方式。
