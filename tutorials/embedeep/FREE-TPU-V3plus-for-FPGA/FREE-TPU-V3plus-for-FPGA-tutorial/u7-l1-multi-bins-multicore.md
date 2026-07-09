# multi_bins_test：多网络与多核实例

## 1. 本讲目标

本讲是「进阶 demo」单元的第一篇。在前面你已经能用 `classify`、`yolo` 等**单网络** demo 跑通一次推理；本讲要解决一个更接近真实产品的问题：

> **能不能在一个进程里，同时加载并运行两个（甚至更多）不同的网络？**

`multi_bins_test` 这个 demo 就是答案。读完本讲，你应当能够：

1. 理解**多 `EEPTPU` 实例**的创建方式：同一个进程里 new 出两个独立的 TPU 对象，各自加载各自的 bin。
2. 掌握**多核寄存器 zone（`core_id` 0/1）**的配置：为什么多实例时要注册第二个核的寄存器窗口。
3. 读懂 **`fg_multi` 基地址策略**：第一个 bin 和后续 bin 在 DDR 基地址设置上的差别。
4. 认识多网络共享进程时的**资源管理**：谁负责 `close`、谁负责释放结果、两个网络的输入互不干扰。

本讲承接 [u2-l3（EEPTPU 运行库 API 与 classify demo）](u2-l3-linux-api-and-classify.md) 的初始化「三件套」与 [u6-l2（目标检测后处理）](u6-l2-yolo-detect-postprocess.md) 的检测表解析，不再重复单实例推理流程，而是聚焦在「多了一个实例」之后带来的所有变化点。

---

## 2. 前置知识

在进入源码前，先用三段话补齐本讲要用到的概念。

### 2.1 回顾：一个 EEPTPU 实例由什么构成

在 u2-l3 里我们确立过：Linux 路线下的推理对象是 `EEPTPU` 类（来自闭源运行库 `libeeptpu_pub`）。一个 `EEPTPU` 实例内部持有：接口类型、寄存器 zone 列表、DDR 基地址、加载进来的 bin（含权重、调度表、地址表）、以及 `input_shape`（NCHW）。典型调用顺序是：

```
init() → set_base_address / set_tpu_reg_zones → set_interface → load_bin → set_input → forward → 读结果 → close
```

单实例 demo（classify/yolo/icnet）全程只有**一个**全局指针 `static EEPTPU *tpu`。本讲的全部「新东西」，都源自把这个「一个」变成「两个」。

### 2.2 什么是「核」与「寄存器 zone」

EEP-TPU V3+ 是一个**多核**处理器（README 强调 *Multi-core with Multithreading technology*，八核 MobileNetV2 INT8 可到 0.56 ms）。在 ZynqMP 的地址地图里，每个 TPU 核都有一块**独立的控制寄存器窗口**：

- core 0 的寄存器窗口落在物理地址 `0xA0000000`
- core 1 的寄存器窗口落在物理地址 `0xA0040000`（两者相距 `0x40000`，即 256 KB，每个核独占一个 256 KB 窗口）

软件驱动一个核，本质就是往这个核的寄存器窗口里写命令（参见 u4-l2/u5-l1 的 `STARTUP=0x11`、`STATUS bit31` 协议）。要让运行库能驱动某个核，就必须先用 `eeptpu_set_tpu_reg_zones()` 把那个核的窗口地址**注册**给它——这就是 `EEPTPU_REG_ZONE{core_id, addr, size}` 的意义。

> 一句话：**注册哪个 zone，运行库就能驱动哪个核**。单核 demo 只注册 core 0；多核/多网络场景需要把更多 zone 注册进去。

### 2.3 为什么多个网络不能「随便塞」

每个 bin 加载后，它的权重和中间张量都要放在 DDR 的某段物理地址上。如果两个网络都硬指定同一段 DDR，后加载的就会**覆盖**前一个的数据。因此多网络共存时，必须有一种「基地址协调」机制——这正是 `fg_multi` 这个标志位要解决的问题。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `sdk/demo/multi_bins_test/main.cpp` | 本讲主角：创建两个 EEPTPU 实例、配多核 zone、按 `fg_multi` 策略初始化、依次跑分类与检测 |
| `sdk/demo/multi_bins_test/compile.sh` | 交叉编译脚本，结构与 classify 几乎一致，但**不**编 npy 相关文件 |
| `sdk/demo/multi_bins_test/test.sh` | 运行脚本，传入**两个** bin（mobilenet + yolov4tiny）和一张图 |
| `sdk/demo/classify/main.cpp` | 对照组：单实例、单 bin 的初始化写法 |
| `sdk/demo/yolo/main.cpp` | 对照组：单实例下 core 1 zone 被注释掉的写法 |
| `README.md` | 多核多线程的性能说明 |

> 说明：`EEPTPU` 类、`EEPTPU_REG_ZONE` 结构体的定义都在闭源头文件 `eeptpu.h` 中（属于 `libeeptpu_pub`，本仓库不提供，需按 `sdk/Readme.md` 单独下载预编译库）。本讲完全依据 demo 中的**实际调用**来推断 API 语义，凡涉及库内部实现（如 base 地址内部如何分配、bin 如何路由到具体核）一律标注「待确认」，不编造。

---

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：**双 EEPTPU 实例**、**多核寄存器 zone**、**`fg_multi` 基地址策略**、**分类+检测协同执行与资源管理**。

### 4.1 双 EEPTPU 实例：从「一个 tpu」到「两个 tpu」

#### 4.1.1 概念说明

单网络 demo 用一个全局指针管一个网络。要让两个网络共存，最自然的做法不是在一个对象里塞两套权重，而是**创建两个独立的 `EEPTPU` 对象**，各自走一遍完整的 init→load→forward 生命周期。两个对象互不干扰：各有各的 bin、各有各的 `input_shape`、各有各的输出结果。

#### 4.1.2 核心流程

```text
声明两个空指针 tpu_classify / tpu_det
        │
        ├── eeptpu_init(tpu_classify, ..., fg_multi=0)   # 第一个实例
        │       └─ tpu->init()  工厂方法 new 出对象
        │       └─ set zones / set_interface / load_bin
        │
        └── eeptpu_init(tpu_det,     ..., fg_multi=1)   # 第二个实例
                └─ tpu->init()  再 new 出一个对象
                └─ set zones / set_interface / load_bin(, fg_multi=1)
```

关键点：`eeptpu_init` 被设计成**可复用**的函数——它接受「实例引用」`EEPTPU*& tpu` 和一个 `fg_multi` 标志，于是同一段初始化代码能为两个实例各服务一次。

#### 4.1.3 源码精读

先看两个全局实例的声明，注意它们和 classify 里那一个 `static EEPTPU *tpu` 的对比：

[sdk/demo/multi_bins_test/main.cpp:12-13](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L12-L13) —— 声明两个独立的 EEPTPU 指针，一个用于分类网络，一个用于检测网络。

对比 classify 的单指针写法 [sdk/demo/classify/main.cpp:15](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L15) —— 全进程只有一个 `tpu`，这是单实例与多实例最直观的差别。

再看 `eeptpu_init` 的签名，它比 classify 版多了「实例引用」和 `fg_multi` 两个参数：

[sdk/demo/multi_bins_test/main.cpp:52-56](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L52-L56) —— `EEPTPU*& tpu` 用引用传出新建的对象；`if (tpu == NULL) tpu = tpu->init();` 里 `init()` 是工厂方法（返回一个新建的 `EEPTPU*`，与 classify 第 33 行同一写法）。这一句只在指针为空时才构造对象，保证两次调用各 new 一个独立实例。

而 classify 的 `eeptpu_init` 不需要这两个参数，因为它服务的是固定的那一个全局 `tpu`：[sdk/demo/classify/main.cpp:29-33](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L29-L33)。

最后看 `main` 里两次调用，注意 `fg_multi` 一个传 0、一个传 1：

[sdk/demo/multi_bins_test/main.cpp:174-190](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L174-L190) —— 先初始化分类实例（`fg_multi=0`），打印它的 `input_shape`；再初始化检测实例（`fg_multi=1`），打印它**不同**的 `input_shape`。两个实例加载不同 bin 后，`input_shape` 自然不同（mobilenet 与 yolov4tiny 的输入尺寸不一样），这正说明它们彼此独立。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认「两个实例彼此独立、各持有自己的上下文」。
2. **操作步骤**：打开 `multi_bins_test/main.cpp`，在 `main` 的两次 `eeptpu_init` 之后各加一行打印（仅设计，不实际改源码也可）：
   ```cpp
   printf("classify instance ptr = %p\n", (void*)tpu_classify);
   printf("detect   instance ptr = %p\n", (void*)tpu_det);
   ```
3. **需要观察的现象**：两个指针值不同 → 证明是两个独立对象。
4. **预期结果**：两个地址互不相同，且两次打印的 `input_shape` 数值也不同（分类网络与检测网络输入分辨率不同）。
5. 运行需上板，无板时为「待本地验证」。

#### 4.1.5 小练习与答案

- **Q1**：`eeptpu_init` 为什么用 `EEPTPU*& tpu`（指针的引用）而不是 `EEPTPU* tpu`（值传递指针）？
  - **A**：因为函数内部要执行 `tpu = tpu->init()` 给这个指针**赋一个新值**（指向新建对象）。值传递只会改副本，调用方的 `tpu_classify` 仍是 NULL；用引用才能把新对象回传给调用方。
- **Q2**：如果想让这个 demo 同时跑三个网络，需要改哪些地方？
  - **A**：再声明第三个全局指针、再调用一次 `eeptpu_init`（`fg_multi` 同样用 1），并在 `main` 末尾补上对应的 `close`/`delete`。第一个实例始终 `fg_multi=0`，后续都用 `fg_multi=1`。

---

### 4.2 多核寄存器 zone：注册 core 0 与 core 1

#### 4.2.1 概念说明

第 2.2 节说过，运行库能驱动哪些核，取决于你给它注册了哪些 `EEPTPU_REG_ZONE`。单网络 demo 只用一个核，所以只注册 core 0；一旦要在多网络/多核场景下运行，就需要把第二个核的窗口地址也注册进去，让运行库「看见」core 1。

#### 4.2.2 核心流程

```text
构造 vector<EEPTPU_REG_ZONE> regzones
   ├── push core 0 : { core_id=0, addr=0xA0000000, size=0x1000 }
   └── push core 1 : { core_id=1, addr=0xA0040000, size=0x1000 }   ← 多实例才加这行
eeptpu_set_tpu_reg_zones(regzones)   ← 一次把两个核的窗口都交给运行库
```

注意 `size=0x1000`（4 KB）是软件实际映射的窗口大小；硬件层面每个核预留的是 256 KB（见 u1-l4 的 `assign_bd_address`），二者不矛盾——软件只映射自己要用的那一小块。

#### 4.2.3 源码精读

看 multi_bins_test 在 SoC/arm64 分支里注册了**两个** zone：

[sdk/demo/multi_bins_test/main.cpp:81-86](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L81-L86) —— 同时 push core 0（`0xA0000000`）和 core 1（`0xA0040000`），把两核的寄存器窗口都注册给运行库。这一段在两次 `eeptpu_init` 中都会执行，即两个实例都被知会了完整的多核布局。

对比 classify 的写法，core 1 那一行是**被注释掉**的：

[sdk/demo/classify/main.cpp:57-60](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L57-L60) —— 单实例只需要 core 0，core 1 的注册行被注释，运行库自然也就只能驱动一个核。

yolo demo 也是同样的对照：[sdk/demo/yolo/main.cpp:116-118](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L116-L118) —— core 1 同样被注释，留作需要时打开。

> 还有一处细节：PCIE 模式下，multi_bins_test 只注册了 core 0（[main.cpp:67-70](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L67-L70)），窗口大小用的是 `256*1024`、地址是 `0x00040000`。这说明「注册哪些核」与硬件形态有关，不同板卡/PCIe 配置下窗口地址不同，需按实际硬件调整（具体地址含义见 u2-l4）。

#### 4.2.4 代码实践（源码阅读 + 推理型）

1. **实践目标**：理解「注册 zone = 告诉运行库核的物理位置」。
2. **操作步骤**：
   - 在 `multi_bins_test/main.cpp` 第 84、85 行旁加注释，标出 `0xA0000000` 是 core 0、`0xA0040000` 是 core 1。
   - 计算 `0xA0040000 - 0xA0000000 = 0x40000`（256 KB），印证「每核独占 256 KB 窗口」。
3. **需要观察的现象**：两个核的窗口地址之差恰好等于一个核窗口的预留大小。
4. **预期结果**：差值 = `0x40000` = 262144 字节 = 256 KB，与硬件 `assign_bd_address` 的预留一致。
5. 若你手上的硬件只有单核 TPU，则 core 1 这一行注册后行为为「待本地验证」（可能无效或被库忽略）。

#### 4.2.5 小练习与答案

- **Q1**：如果把 core 1 那行也注释掉（只留 core 0），multi_bins_test 还能跑吗？
  - **A**：取决于运行库如何把两个 bin 路由到核。若库能用单核分时复用跑两个 bin，可能仍可跑（顺序执行）；若库要求第二个 bin 必须落到 core 1，则会失败。该路由策略属库内部实现，**待确认**。但可以确定的是：注释掉 core 1 后，运行库就失去了直接驱动 core 1 的能力。
- **Q2**：`size=0x1000` 与硬件预留的 256 KB 是什么关系？
  - **A**：硬件给每个核预留了 256 KB 地址空间（地址不重叠），但 TPU 寄存器实际只用了很靠前的少量偏移（如 `STATUS@0x0C`、`STARTUP@0x34`、`BASEADDR@0x50` 等，见 u5-1），所以软件只需映射 `0x1000`（4 KB）窗口即可覆盖全部用到的寄存器。

---

### 4.3 `fg_multi` 基地址策略：第一个 bin 与后续 bin 的差别

#### 4.3.1 概念说明

`fg_multi` 是 multi_bins_test 区别于单实例 demo 的**核心标志位**。它解决的问题是：多个 bin 在 DDR 里如何摆才不打架。规则很简洁——

- **第一个 bin（`fg_multi=0`）**：由用户**显式**指定 DDR 基地址（`eeptpu_set_base_address`）。
- **后续 bin（`fg_multi=1`）**：**跳过**显式 `set_base_address`，把 `fg_multi=1` 传给 `eeptpu_load_bin`，把基地址的协调**交给运行库**。

换句话说，`fg_multi` 是一条「我是不是第一个网络」的标记：第一个网络先把 DDR 落脚点定下来，后面的网络让库去安排，避免和前者撞在同一块 DDR。

#### 4.3.2 核心流程

```text
eeptpu_init(tpu, ..., fg_multi):
    ... set zones, set_interface ...
    if (fg_multi == 0):
        eeptpu_set_base_address(0x40000000, 0x40000000, 0x40000000, 0x40000000)  # 显式落点
    eeptpu_load_bin(path_bin, fg_multi)   # 把 fg_multi 也告诉 load_bin
```

四个相同的 `0x40000000` 分别对应 par/in/tmp/out 四段内存基址（与裸机 config.h 的 `BASEADDR0~3` 同构，见 u5-l1）。第一段是参数/权重、第二段是输入、第三段是临时、第四段是输出——单实例时四段可以重叠落在同一基址（由库内部偏移区分），多实例时则靠 `fg_multi` 协调。

#### 4.3.3 源码精读

最关键的一段——`fg_multi` 守卫了 `set_base_address`：

[sdk/demo/multi_bins_test/main.cpp:83-92](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L83-L92) —— `#if 1`（arm64）分支里，注册完两个 zone 后，只有 `if (fg_multi == 0)` 时才调用 `eeptpu_set_base_address(0x40000000, ...)`。于是 `tpu_classify`（`fg_multi=0`）显式落在 `0x40000000`；`tpu_det`（`fg_multi=1`）**不**调用这一句。

紧接着，`fg_multi` 又被原样传给 `load_bin`：

[sdk/demo/multi_bins_test/main.cpp:112-117](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L112-L117) —— `eeptpu_load_bin(path_bin, fg_multi)`。对比 classify 的 [main.cpp:81](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L81) 是 `eeptpu_load_bin(path_bin)`（单参数）——多实例版多了一个标志位参数。

还有一个小细节：版本信息只在 `fg_multi==0` 时打印一次，避免两个实例重复刷屏：

[sdk/demo/multi_bins_test/main.cpp:105-110](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L105-L110) —— 库版本/硬件版本/硬件信息只在第一个实例初始化时打印。

> **关于第二个 bin 的 DDR 落点**：源码没有显式给出 `tpu_det` 的基地址，它的真实 DDR 偏移由 `libeeptpu_pub` 在 `load_bin(..., fg_multi=1)` 内部决定（可能是与第一个 bin 不同的区段，也可能库做了分时复用）。该内部策略在闭源库里，**待确认**，我们不臆测具体地址。

#### 4.3.4 代码实践（直接对应任务题）

这是本讲规格里要求的对比实践。

1. **实践目标**：说清单实例与多实例在 `base_address` 设置策略上的差别。
2. **操作步骤**：并排打开两份文件——
   - `sdk/demo/classify/main.cpp` 的 `eeptpu_init`（[第 51-64 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L51-L64)）
   - `sdk/demo/multi_bins_test/main.cpp` 的 `eeptpu_init`（[第 83-92 行](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L83-L92)）
3. **需要观察的现象 / 结论**：
   - **classify**：无条件调用 `eeptpu_set_base_address(0x40000000, ...)`，因为只有一个 bin，它的落脚点用户说了算。
   - **multi_bins_test**：用 `if (fg_multi == 0)` 把 `set_base_address` 包起来——只有第一个实例（分类）显式落在 `0x40000000`；第二个实例（检测）跳过显式设置，改由 `load_bin(..., fg_multi=1)` 让库内部协调，从而避免两个网络的张量在 DDR 里互相覆盖。
   - **为何要注册 core 1 的 zone**：因为第二个网络要让运行库能驱动到第二个核（或多核调度），必须把 core 1 的窗口 `0xA0040000` 注册进去；单实例 demo 用不到第二个核，所以那一行被注释。
4. **预期结果**：能用一句话答出——「第一个 bin 显式定基址、后续 bin 让库分配；注册 core 1 是为多核/多网络打开第二核的访问通道」。
5. 无板时为「待本地验证」运行结果，但源码层面的对比结论是确定的。

#### 4.3.5 小练习与答案

- **Q1**：如果两个实例都传 `fg_multi=0`（即第二个实例也显式 `set_base_address(0x40000000,...)`），会发生什么？
  - **A**：两个网络的张量都会被摆到同一块 DDR `0x40000000` 起，第二个 `load_bin` 很可能覆盖第一个的权重/中间结果，导致分类结果错乱或推理失败。这正是 `fg_multi` 要规避的「DDR 撞车」。
- **Q2**：`eeptpu_set_base_address` 的四个参数为什么本例里全填一样的 `0x40000000`？
  - **A**：它们是 par/in/tmp/out 四段的基址。单实例下库会在这一基址内部用不同偏移区分四段，所以外部给同一个起点即可；具体段内偏移由 bin 的地址表（编译时烤入，见 u3-l1/u3-l3）决定。

---

### 4.4 分类 + 检测协同执行与资源管理

#### 4.4.1 概念说明

两个实例都初始化好之后，`main` 把它们用起来。本 demo 里分类与检测是**顺序执行**的（先分类、后检测，在同一个线程里），并非真正并发——但它们已经「在同一个进程里共存」。这里要关注的是**资源管理**：输入要分别喂、结果要分别释放、退出时两个实例都要 `close`。

#### 4.4.2 核心流程

```text
main:
  eeptpu_init(tpu_classify, ..., 0)      # 实例1 就绪
  eeptpu_init(tpu_det,     ..., 1)       # 实例2 就绪
  classify_forward_test(image)           # 用 tpu_classify：写输入→forward→top5
  objdet_forward_test(image)             # 用 tpu_det：     写输入→forward→画框存图
  tpu_classify->close(); delete          # 收尾：关实例1
  tpu_det->close();     delete           # 收尾：关实例2
```

注意 `eeptpu_write_input` 被设计成接受「哪个 tpu」作参数，于是同一张图能按各自 `input_shape` 分别预处理后喂给两个网络。

#### 4.4.3 源码精读

顺序调用两个测试函数：

[sdk/demo/multi_bins_test/main.cpp:195-207](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L195-L207) —— 先 `classify_forward_test`，再 `objdet_forward_test`，任一失败即返回。两者共享同一张输入图 `path_image`，但走各自的实例。

输入预处理按实例参数化——注意它读的是 `tpu->input_shape`（当前实例的形状）：

[sdk/demo/multi_bins_test/main.cpp:124-145](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L124-L145) —— `eeptpu_write_input(EEPTPU* tpu, ...)` 根据该实例的 `input_shape[1]`（通道数）决定按 BGR 还是 GRAY 读图，并 resize 到该实例的 `(W,H)`，再 `set_input`。所以同一张图被两次、按不同尺寸喂给了两个网络。

分类这一路（复用 u2-l3/u6-l1 的 topk 逻辑）：

[sdk/demo/multi_bins_test/main.cpp:233-258](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L233-L258) —— `tpu_classify->eeptpu_forward(result)` 后取 top5；用 `get_current_time()` 量端到端墙钟，用 `eeptpu_get_tpu_forward_time()` 量纯硬件耗时（与 classify demo 一致）。

检测这一路（复用 u6-l2 的检测表解析与画框）：

[sdk/demo/multi_bins_test/main.cpp:276-309](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L276-L309) —— `tpu_det->eeptpu_forward(result)`，再 `post_process_obj_detect` 解析 `[1,1,N,6]` 检测表，`draw_objects` 画框画字，`eepimg_save` 存成 `./objdet.jpg`。

退出时的资源回收——两个实例都要关：

[sdk/demo/multi_bins_test/main.cpp:209-214](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L209-L214) —— 分别 `eeptpu_close()` 释放运行库内部资源、`delete` 释放 C++ 对象，再 `clear_objects()` 清空检测得到的全局 `g_objects`。**漏关任何一个实例都是资源泄漏**。

> 关于「并发」：本 demo 在**单线程**里顺序跑两个网络（banner 写的是 `EEP-TPU 1 Core & 2 Bins`，见 [main.cpp:172](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L172)）。已注册的双核 zone 是**前提条件**，让运行库具备多核调度能力；是否真正把两个网络分到两个核上并行，由库内部决定（**待确认**）。如果你要追求真并行，可在应用层用线程分别驱动两个实例，但须自行保证两实例间没有共享可变状态——本 demo 里它们除了都读同一张输入图、共用 `g_objects`（仅检测写）外，彼此独立。

#### 4.4.4 代码实践（源码阅读 + 设计型）

1. **实践目标**：把「双实例协同 + 资源管理」串成一条完整链路。
2. **操作步骤**：
   - 在 `main.cpp` 里用纸笔（或注释）标注数据流：`path_image` →（按 classify 形状）→ `tpu_classify` → top5；同一张 `path_image` →（按 det 形状）→ `tpu_det` → `objdet.jpg`。
   - 设计一个改动：把 `classify_forward_test` 与 `objdet_forward_test` 放进两个 pthread 各跑一次，用 `gettimeofday` 量「并行后总耗时」相比顺序执行的降幅。
3. **需要观察的现象**：顺序执行时总耗时 ≈ 分类耗时 + 检测耗时；若库能把两网络调度到不同核，并行后总耗时接近两者中较大者。
4. **预期结果**：并行是否带来加速取决于硬件核数与库的调度（**待本地验证**）。但无论如何，线程化时必须确保两个 `EEPTPU*` 实例各自独立使用（本 demo 已满足），且不要在两线程里同时写 `g_objects`。
5. 无板/无双核硬件时为「待本地验证」。

#### 4.4.5 小练习与答案

- **Q1**：为什么 `eeptpu_write_input` 要把 `EEPTPU* tpu` 当参数传进来，而 classify demo 里不需要？
  - **A**：因为现在有两个实例，它们的 `input_shape` 不同（分类与检测输入尺寸不同）。必须告诉函数「这次喂给哪个实例」，它才能按正确的通道数和尺寸去读图、resize、`set_input`。classify 只有一个全局 `tpu`，自然不需要传。
- **Q2**：退出时只 `delete(tpu_classify)` 忘了 `delete(tpu_det)`，会有什么后果？
  - **A**：检测实例占用的运行库内部资源（寄存器映射、bin 缓冲等）不会随进程退出被显式归还，属于资源泄漏；在长生命周期的服务进程里反复加载会耗尽资源。规范做法是像 demo 末尾那样对每个实例都 `eeptpu_close()` + `delete`。

---

### 4.5 构建与运行：compile.sh 与 test.sh

#### 4.5.1 概念说明

本模块不是新的算法点，而是把上面的双实例程序「编出来、跑起来」的工具链。它和 classify 的 `compile.sh` 几乎一样，只差两点：不编 npy 文件、运行时要传**两个** bin。

#### 4.5.2 源码精读

编译输入文件——注意它**没有** `cnpy.cpp`/`npy_load.cpp`（因为 multi_bins_test 不读 npy，只读 jpg）：

[sdk/demo/multi_bins_test/compile.sh:32-40](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/compile.sh#L32-L40) —— `input_files` 只有 `main.cpp` 和 `eep_image.cpp`；链接 `-leeptpu_pub`，开启 `-fopenmp`（运行库内部的多线程依赖 OpenMP，与 README 的 *Multi-core with Multithreading* 呼应）。

平台切换与 classify 完全一致：[compile.sh:17-26](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/compile.sh#L17-L26) —— `32/64/86` 分别选 `arm-linux-gnueabihf-g++ / aarch64-linux-gnu-g++ / g++`，并据此选 `../libs/${pf}/eep/lib` 下的预编译库（库本身需按 `sdk/Readme.md` 单独下载，仓库不含）。

运行脚本——注意 `sudo ./demo` 后跟了**两个** bin 再加图片：

[sdk/demo/multi_bins_test/test.sh:1-3](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/test.sh#L1-L3) —— 第一个 bin 是 `eeptpu_s2_mobilenet_v1.pub.bin`（分类），第二个是 `eeptpu_s2_yolov4tiny.pub.bin`（检测），对应 `main` 的 `argv[1]/argv[2]/argv[3]`。`sudo` 是因为 SoC 模式要映射物理地址（见 u2-l4）。

#### 4.5.3 代码实践（源码阅读型）

1. **实践目标**：搞清「双 bin 命令行」与程序参数的对应。
2. **操作步骤**：对照 [main.cpp:159-168](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/multi_bins_test/main.cpp#L159-L168) 的 `argc != 4` 校验，把 `test.sh` 那行命令的三个参数一一对应到 `path_bin_classify / path_bin_detect / path_image`。
3. **预期结果**：`argv[0]=demo`、`argv[1]=mobilenet bin`、`argv[2]=yolov4tiny bin`、`argv[3]=dog-Husky_248.jpg`，共 4 个 `argv`，与 `argc != 4` 的判断吻合。
4. 实际编译运行需先下载预编译库与 bin，无板时为「待本地验证」。

---

## 5. 综合实践

**任务：为 multi_bins_test 画一张「双实例全生命周期」图，并设计一个把检测实例改到 core 1 的方案。**

1. **画生命周期图**：在一张图上标出两条平行链路——
   - `tpu_classify`：`init` → 注册 zone(core0+core1) → `set_base_address(0x40000000)`（因 `fg_multi=0`）→ `set_interface` → `load_bin(,0)` → `set_input`(按 classify 形状) → `forward` → top5 → `close`
   - `tpu_det`：`init` → 注册 zone(core0+core1) → **跳过** `set_base_address`（因 `fg_multi=1`）→ `set_interface` → `load_bin(,1)` → `set_input`(按 det 形状) → `forward` → 画框 → `close`
   - 在两条链路上分别标出「哪里不同」（base 地址、load_bin 第二参数、input_shape、输出后处理）。
2. **设计改核方案**：假设你的硬件是双核 TPU，想把检测网络固定到 core 1 跑。基于本讲已学的 zone 注册机制，写出你的改动思路（提示：core 1 的窗口已经注册了 `0xA0040000`；如何「告诉」某个实例用 core 1，属 `libeeptpu_pub` 内部 API，源码不可见，故标注「待确认」需查 `eeptpu.h`/ug053 手册）。
3. **资源自检**：检查你的方案里，两个实例退出时是否都被 `close`+`delete`，两个 `EEPTPU_RESULT` 的 `data` 是否都被 `free`（参见 `results_release`）。
4. **预期产出**：一张标注完整的双实例时序图 + 一段「如何指定核」的方案说明（含「待确认」项）。
5. 无板时为设计型实践，结论可在源码层面确定，运行效果「待本地验证」。

---

## 6. 本讲小结

- **多实例 = 多个独立 `EEPTPU` 对象**：`multi_bins_test` 用 `tpu_classify`、`tpu_det` 两个全局指针，靠 `eeptpu_init(EEPTPU*& tpu, ..., fg_multi)` 复用同一段初始化代码各服务一次。
- **注册 zone = 打开核的访问通道**：单实例只注册 core 0（`0xA0000000`）；多实例把 core 1（`0xA0040000`）也注册进去，让运行库具备驱动第二个核的能力。
- **`fg_multi` 是「我是不是第一个网络」的标记**：第一个 bin（`fg_multi=0`）显式 `set_base_address` 落点；后续 bin（`fg_multi=1`）跳过显式设置、由 `load_bin(...,1)` 让库内部协调 DDR，避免张量互相覆盖。
- **协同执行 + 资源管理**：分类与检测在同一进程里顺序执行，输入按各自 `input_shape` 分别预处理；退出时两个实例都要 `eeptpu_close()`+`delete`，结果缓冲都要 `free`。
- **工具链差异**：`compile.sh` 不编 npy 文件；`test.sh` 传入**两个** bin（mobilenet + yolov4tiny）+ 一张图，`main` 用 `argc==4` 校验。
- **诚实边界**：`EEPTPU` 类定义在闭源 `eeptpu.h` 中；第二个 bin 的确切 DDR 落点、bin 到核的路由策略均属库内部实现，本讲一律标「待确认」，不臆测。

---

## 7. 下一步学习建议

- 想看「单实例却玩出更多花样」（多输入、npy 数据、pack 输出模式），继续本单元的 [u7-l2（nntpu_test：多输入、npy 与 pack 模式）](u7-l2-nntpu-multi-input-npy.md)。
- 想理解多核在硬件侧如何被驱动，回到 [u5-l1（tpu_forward 寄存器时序）](u5-l1-forward-register-timing.md) 与 [u4-l2（EEPTPU_SA 与寄存器协议）](u4-l2-eeptpu-sa-and-register-protocol.md)，对照 `BASEADDR/STARTUP/STATUS` 看「写地址→启动→轮询」如何落到具体核。
- 想做真并行，建议阅读闭源 `eeptpu.h`（需从 SDK 获取）确认是否有指定 `core_id` 的 API，再结合本讲的线程化设计落地。
- 性能维度上，可结合 [README.md:48](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/README.md#L48) 的「八核 MobileNetV2 INT8 0.56 ms」理解多核多线程对延迟的实际收益。
